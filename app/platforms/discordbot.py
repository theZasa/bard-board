"""Discord adapter: discord.py behind the platform interface.

One bot serves one Discord server (guild). Commands arrive as chat messages ("!sb leeroy") and as
slash commands generated from the same command list. Audio comes from the mixer as 20 ms mono
frames and is played through a pull based discord.py AudioSource.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

import discord
import numpy as np
from discord import app_commands

from ..opus import FRAME_SAMPLES
from .base import ChannelId, ChannelInfo, IncomingText, PlatformUser
from .markdown import EMBED_LIMIT, escape_markdown, html_to_markdown, split_message
from .mumble import strip_html

if TYPE_CHECKING:
    from ..config import Config
    from ..core import Soundboard

log = logging.getLogger("discordbot")

STEREO_FRAME_BYTES = FRAME_SAMPLES * 2 * 2  # 20 ms, 48 kHz, 16 bit, stereo
PREBUFFER_FRAMES = 3  # cushion against scheduling jitter between the mixer and discord.py's player
MAX_QUEUED_FRAMES = 25  # never lag more than half a second behind the mixer
READ_TIMEOUT = 0.06
CONNECT_TIMEOUT = 20.0
URL = re.compile(r"<?(https?://[^\s<>]+)>?")
SLASH_NAME = re.compile(r"^[-_\w]{1,32}$")
OPTION = "eingabe"
VOICE_TYPES: tuple[type, ...] = (discord.VoiceChannel,)  # stage channels would make the bot a listener

BRAND = discord.Colour(0x7C5CFF)  # the web interface's accent colour
LIVE = discord.Colour(0xE03347)
NO_MENTIONS = discord.AllowedMentions.none()


def embeds_for(html_text: str) -> list[discord.Embed]:
    """A reply as embeds: coloured bar, proper links, room for long lists (4096 characters each)."""
    text = html_to_markdown(html_text)
    return [discord.Embed(description=chunk, colour=BRAND) for chunk in split_message(text, EMBED_LIMIT)]


def track_embed(info: Any, requested_by: str) -> discord.Embed:
    """ "Now playing" card: linked title, length or LIVE, who asked for it, thumbnail."""
    url = info.webpage if str(info.webpage).startswith(("http://", "https://")) else None
    embed = discord.Embed(title=info.title[:256], url=url, colour=LIVE if info.is_live else BRAND)
    embed.set_author(name="🎵 Jetzt läuft")
    parts = []
    if info.is_live:
        parts.append("🔴 **LIVE**")
    elif info.duration:
        minutes, seconds = divmod(int(info.duration), 60)
        hours, minutes = divmod(minutes, 60)
        parts.append(f"⏱ {hours}:{minutes:02d}:{seconds:02d}" if hours else f"⏱ {minutes}:{seconds:02d}")
    if info.uploader:
        parts.append(escape_markdown(info.uploader))
    if parts:
        embed.description = " · ".join(parts)
    embed.set_footer(text=f"gewünscht von {requested_by}")
    if info.thumbnail and str(info.thumbnail).startswith("https://"):
        embed.set_thumbnail(url=info.thumbnail)
    return embed


INVITE_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    send_messages_in_threads=True,
    read_message_history=True,
    connect=True,
    speak=True,
    use_voice_activation=True,
)


# --------------------------------------------------------------------------- audio


class PcmSource(discord.AudioSource):
    """Frames pushed by the mixer (event loop thread), read by discord.py's player thread."""

    def __init__(self) -> None:
        self._frames: deque[bytes] = deque()
        self._cond = threading.Condition()
        self._ending = False  # no more frames will come
        self.finished = False  # read() reported the end, the player is gone
        self.dropped = 0

    def push(self, frame: bytes) -> bool:
        """Queue a stereo frame. False when the source already ended (a new one is needed)."""
        with self._cond:
            if self.finished:
                return False
            self._ending = False  # more audio right after an end: keep playing seamlessly
            self._frames.append(frame)
            while len(self._frames) > MAX_QUEUED_FRAMES:
                self._frames.popleft()
                self.dropped += 1
            self._cond.notify()
            return True

    def end(self) -> None:
        with self._cond:
            self._ending = True
            self._cond.notify()

    @property
    def queued(self) -> int:
        return len(self._frames)

    @property
    def ending(self) -> bool:
        return self._ending

    def read(self) -> bytes:
        with self._cond:
            if not self._frames and not self._ending:
                self._cond.wait(READ_TIMEOUT)
            if self._frames:
                return self._frames.popleft()
            if self._ending or self.finished:
                self.finished = True
                return b""
            # The mixer is late: a short gap of silence instead of stopping the player.
            return bytes(STEREO_FRAME_BYTES)

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        with self._cond:
            self.finished = True
            self._frames.clear()


def to_stereo(pcm: bytes) -> bytes:
    mono = np.frombuffer(pcm, dtype=np.int16)
    return np.repeat(mono, 2).tobytes()


def ensure_opus() -> None:
    if discord.opus.is_loaded():
        return
    from .. import opus as our_opus  # already found libopus for the Mumble encoder

    discord.opus.load_opus(our_opus._lib._name)


def describe_error(exc: BaseException) -> str:
    """Short German text for the status line."""
    if isinstance(exc, discord.HTTPException):
        return f"Discord meldet Fehler {exc.status}" + (f": {exc.text}" if exc.text else "")
    if isinstance(exc, (OSError, asyncio.TimeoutError)) or type(exc).__module__.startswith("aiohttp"):
        return "Discord ist nicht erreichbar (Internetverbindung?)"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def voice_problem() -> str:
    """Why the bot cannot speak in voice channels, or "" if it can."""
    from discord import voice_client, voice_state

    missing = [name for name, ok in (("PyNaCl", voice_client.has_nacl), ("davey", voice_state.has_dave)) if not ok]
    if missing:
        verb = "fehlt" if len(missing) == 1 else "fehlen"
        return f"Sprachkanäle gehen nicht: Python-Paket {' und '.join(missing)} {verb} (Image neu bauen)"
    return ""


class DiscordAudio:
    """The mixer's sink: starts a discord.py player per burst of audio."""

    def __init__(self, platform: "DiscordPlatform") -> None:
        self.platform = platform
        self._source: PcmSource | None = None
        self._playing: PcmSource | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def ready(self) -> bool:
        vc = self.platform.voice_client
        return vc is not None and vc.is_connected()

    def send_frame(self, pcm: bytes) -> None:
        vc = self.platform.voice_client
        if vc is None or not vc.is_connected():
            return
        self._loop = asyncio.get_running_loop()
        frame = to_stereo(pcm)
        if self._source is None or not self._source.push(frame):
            self._source = PcmSource()
            self._source.push(frame)
        self._start(vc)

    def end(self) -> None:
        if self._source is not None:
            self._source.end()
            vc = self.platform.voice_client
            if vc is not None:
                self._start(vc)  # a burst shorter than the prebuffer still has to play

    async def drain(self) -> None:
        return None

    def _start(self, vc: Any) -> None:
        src = self._source
        if src is None or src.finished:
            return
        if self._playing is src and vc.is_playing():
            return
        if src.queued < PREBUFFER_FRAMES and not src.ending:
            return
        try:
            vc.play(src, after=self._after, application="audio", bitrate=self.platform.bitrate_kbps(),
                    signal_type="music")
            self._playing = src
        except discord.ClientException:
            pass  # the previous player is just finishing: try again with the next frame
        except discord.opus.OpusNotLoaded:
            log.error("libopus nicht geladen - keine Audioausgabe möglich")

    def _after(self, error: Exception | None) -> None:
        # Runs in the player thread when a player stops.
        if error is not None:
            log.warning("Audiowiedergabe abgebrochen: %s", error)
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._kick)

    def _kick(self) -> None:
        """A burst that could not start while the previous player was finishing gets its turn now."""
        vc = self.platform.voice_client
        if vc is not None and vc.is_connected():
            self._start(vc)

    def reset(self) -> None:
        if self._source is not None:
            self._source.cleanup()
        self._source = self._playing = None


# ------------------------------------------------------------------- the platform


def _member_user(member: Any) -> PlatformUser:
    voice = getattr(member, "voice", None)
    channel = voice.channel if voice is not None else None
    return PlatformUser(
        id=str(member.id),
        name=member.display_name,
        channel_id=str(channel.id) if channel is not None else None,
        muted=bool(voice and (voice.self_mute or voice.mute)),
        deaf=bool(voice and (voice.self_deaf or voice.deaf)),
    )


def channel_path(channel: Any) -> str:
    category = getattr(channel, "category", None)
    return f"{category.name}/{channel.name}" if category is not None else channel.name


def plain_help(text: str, prefix: str = "!") -> str:
    return strip_html(text.replace("{p}", prefix))


def slash_options(usage: str) -> tuple[bool, bool, str]:
    """(has option, required, description) for a command's usage text like "&lt;name&gt;"."""
    usage = strip_html(usage).strip()
    if not usage:
        return False, False, ""
    required = usage.startswith("<")
    return True, required, usage.strip("<>[]")[:100] or "Eingabe"


class _Client(discord.Client):
    def __init__(self, platform: "DiscordPlatform", intents: discord.Intents) -> None:
        super().__init__(
            intents=intents,
            activity=discord.CustomActivity(name=platform.activity_text()),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.platform = platform
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self) -> None:
        await self.platform.on_ready()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.platform.on_ready(gateway=False)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.platform.on_ready(gateway=False)

    async def on_message(self, message: discord.Message) -> None:
        await self.platform.on_message(message)

    async def on_voice_state_update(self, member: discord.Member, before: Any, after: Any) -> None:
        await self.platform.on_voice_state_update(member, before, after)


class DiscordPlatform:
    key = "discord"
    name = "Discord"
    message_length = EMBED_LIMIT  # replies are embeds

    def __init__(self, cfg: "Config") -> None:
        self.cfg = cfg
        self.board: "Soundboard | None" = None
        self.client: _Client | None = None
        # The bot may be on several servers (all, or those in DISCORD_GUILD) but in one voice channel at a
        # time: "guild" is where it is (or will be) in voice, "guilds" are all servers it answers on.
        self.guild: Any = None
        self.guilds: list[Any] = []
        self._synced: set[int] = set()
        self.audio = DiscordAudio(self)
        self.last_error = ""
        self.notices: dict[str, str] = {}  # shown above the web interface until fixed
        self.invite_url = ""
        self.connected_since: float | None = None
        self._message_content = True
        self._ever_ready = False
        self._invite_needed = False
        self._closing = False
        self._moving: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._member_cache: dict[int, tuple[float, Any]] = {}
        self._tasks: set[asyncio.Task] = set()

    def bind(self, board: "Soundboard") -> None:
        self.board = board

    def message_size(self, html_text: str) -> int:
        return len(html_to_markdown(html_text))

    def activity_text(self) -> str:
        return f"{self.cfg.chat_prefix}help · /sb"

    # ------------------------------------------------------------- lifecycle

    def _intents(self) -> discord.Intents:
        intents = discord.Intents.default()  # includes guilds, voice states, guild and DM messages
        intents.message_content = self._message_content
        return intents

    async def run_forever(self) -> None:
        ensure_opus()
        missing = voice_problem()
        if missing:
            self.notices["voice"] = missing
            log.error(missing)
        delay = 5.0
        while not self._closing:
            if self._ever_ready:
                delay, self._ever_ready = 5.0, False
            client = self.client = _Client(self, self._intents())
            self._build_commands(client)
            try:
                await client.start(self.cfg.discord_token, reconnect=True)
            except discord.LoginFailure:
                self._fail("Discord lehnt den Token ab - bitte mit zugangsdaten.bat neu eintragen (Discord)")
                await self._close_client(client)
                return  # retrying with the same token cannot help; the web interface keeps running
            except discord.PrivilegedIntentsRequired:
                await self._close_client(client)
                if self._message_content:
                    self._message_content = False
                    self.notices["intent"] = (
                        "Message Content Intent ist im Developer Portal nicht aktiviert: !-Befehle gehen nur per "
                        "Direktnachricht oder @Erwähnung, Slash-Befehle immer (nach dem Einschalten start.bat)")
                    log.warning(self.notices["intent"])
                    continue
                self._fail("Discord verlangt privilegierte Intents, die nicht aktiviert sind")
                return
            except asyncio.CancelledError:
                await self._close_client(client)
                raise
            except Exception as exc:  # noqa: BLE001 - network trouble: try again later
                log.warning("Discord-Verbindung: %s: %s", type(exc).__name__, exc)
                self._fail(describe_error(exc))
            await self._close_client(client)
            if self._closing:
                return
            log.info("Neuer Verbindungsversuch in %.0f s", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300.0)

    def _fail(self, text: str) -> None:
        self.last_error = text
        self.connected_since = None
        log.error("Discord: %s", text)
        if self.board is not None:
            self._spawn(self.board.on_platform_disconnected(text))

    async def _close_client(self, client: _Client) -> None:
        self.audio.reset()
        self.guild = None
        self.guilds = []
        self._synced.clear()
        if not client.is_closed():
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                log.debug("Fehler beim Schließen", exc_info=True)

    async def close(self) -> None:
        self._closing = True
        if self.client is not None:
            await self._close_client(self.client)

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --------------------------------------------------------- gateway events

    def _allowed_guilds(self, guilds: list[Any]) -> list[Any]:
        """The servers the bot answers on: those named in DISCORD_GUILD (comma separated, in that order),
        or all it is on."""
        wanted = [w.strip() for w in self.cfg.discord_guild.split(",") if w.strip()]
        if not wanted:
            return list(guilds)
        allowed = []
        for w in wanted:
            g = next((g for g in guilds if str(g.id) == w or g.name.casefold() == w.casefold()), None)
            if g is not None and g not in allowed:
                allowed.append(g)
        return allowed

    def _guild_by_id(self, guild_id: int | None) -> Any:
        return next((g for g in self.guilds if g.id == guild_id), None)

    def _guilds_current_first(self) -> list[Any]:
        return sorted(self.guilds, key=lambda g: self.guild is None or g.id != self.guild.id)

    @property
    def multi(self) -> bool:
        return len(self.guilds) > 1

    async def on_ready(self, gateway: bool = True) -> None:
        """Gateway ready, or the bot joined/left a server."""
        client = self.client
        if client is None or client.user is None:
            return
        if client.application_id:
            self.invite_url = discord.utils.oauth_url(
                client.application_id, permissions=INVITE_PERMISSIONS, scopes=("bot", "applications.commands"))
        self.guilds = self._allowed_guilds(list(client.guilds))
        current = self._guild_by_id(self.guild.id) if self.guild is not None else None
        guild = current or (self.guilds[0] if self.guilds else None)
        if guild is None:
            if self.guild is not None:
                self.audio.reset()
            self.guild = None
            what = f"Server {self.cfg.discord_guild!r}" if self.cfg.discord_guild else "einem Server"
            self.last_error = f"Der Bot ist noch nicht auf {what}"
            self._invite_needed = True
            log.warning("%s - Einladungslink: %s", self.last_error, self.invite_url)
            self.board.publish_status()
            return
        self.guild = guild
        self.last_error = ""
        self._invite_needed = False
        if self.connected_since is None:
            self.connected_since = time.time()
        new = [g for g in self.guilds if g.id not in self._synced]
        if new:
            log.info("Verbunden als %s auf %s", client.user, ", ".join(g.name for g in self.guilds))
            log.info("Einladungslink: %s", self.invite_url)
            for g in new:
                self._synced.add(g.id)
                self._spawn(self._sync_commands(client, g))
        elif not gateway:
            return
        self._ever_ready = True
        await self.board.on_platform_ready(default_channel=self.cfg.discord_channel,
                                           server=", ".join(g.name for g in self.guilds))

    async def on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        guild = self._guild_by_id(member.guild.id)
        if guild is None or self.guild is None:
            return
        me = guild.me
        is_me = me is not None and member.id == me.id
        if is_me and guild.id != self.guild.id:
            return  # leaving the server the bot just switched away from
        if is_me and after.channel is None:
            self.audio.reset()
        await self.board.on_user_moved(
            _member_user(member),
            old_channel=str(before.channel.id) if before.channel is not None else None,
            is_me=is_me,
        )

    # ---------------------------------------------------------------- state

    @property
    def ready(self) -> bool:
        return self.client is not None and self.client.is_ready() and self.guild is not None

    @property
    def voice_client(self) -> Any:
        return self.guild.voice_client if self.guild is not None else None

    @property
    def me(self) -> PlatformUser | None:
        if self.guild is None or self.guild.me is None:
            return None
        return _member_user(self.guild.me)

    @property
    def channel_id(self) -> ChannelId | None:
        me = self.guild.me if self.guild is not None else None
        voice = getattr(me, "voice", None)
        return str(voice.channel.id) if voice is not None and voice.channel is not None else None

    def _voice_channel(self, channel_id: ChannelId | None) -> Any:
        """A voice channel on any of our servers (Discord IDs are unique across servers)."""
        if not channel_id or not channel_id.isdigit():
            return None
        for guild in self.guilds:
            ch = guild.get_channel(int(channel_id))
            if isinstance(ch, VOICE_TYPES):
                return ch
        return None

    def _path(self, ch: Any) -> str:
        """Kategorie/Kanal, with the server in front when the bot is on several ("Freunde › Lobby")."""
        path = channel_path(ch)
        return f"{ch.guild.name} › {path}" if self.multi else path

    def bitrate_kbps(self) -> int:
        vc = self.voice_client
        channel = getattr(vc, "channel", None)
        bitrate = getattr(channel, "bitrate", 0) or 64000
        return max(16, min(512, bitrate // 1000))

    def users_in_channel(self, channel_id: ChannelId | None) -> list[PlatformUser]:
        ch = self._voice_channel(channel_id)
        if ch is None:
            return []
        return [_member_user(m) for m in ch.members if not m.bot]

    def _member(self, user_id: int) -> Any:
        """The member on one of our servers; where they are in voice if anywhere, else the current server."""
        found = None
        for guild in self._guilds_current_first():
            member = guild.get_member(user_id)
            if member is None:
                continue
            if getattr(getattr(member, "voice", None), "channel", None) is not None:
                return member
            found = found or member
        return found

    def user(self, user_id: str) -> PlatformUser | None:
        if not user_id.isdigit():
            return None
        member = self._member(int(user_id))
        return _member_user(member) if member is not None else None

    def find_user(self, name: str) -> PlatformUser | None:
        wanted = name.strip().removeprefix("@").casefold()
        mention = re.fullmatch(r"<@!?(\d+)>", name.strip())
        if mention:
            return self.user(mention.group(1))
        exact = best = None
        for guild in self._guilds_current_first():
            me_id = guild.me.id if guild.me is not None else None
            for member in guild.members:
                if member.id == me_id:
                    continue
                names = (member.display_name, member.name, getattr(member, "global_name", None) or "")
                if exact is None and member.display_name.casefold() == wanted:
                    exact = member
                if best is None and wanted in (n.casefold() for n in names if n):
                    best = member
        chosen = exact or best
        # The same person on several servers: the one where they are in voice (that is where to follow).
        return _member_user(self._member(chosen.id) or chosen) if chosen is not None else None

    def channels(self) -> list[ChannelInfo]:
        result = []
        for order, guild in enumerate(self.guilds):
            for ch in guild.channels:
                if isinstance(ch, VOICE_TYPES):
                    result.append((order, getattr(ch.category, "position", -1), ch.position,
                                   ChannelInfo(str(ch.id), self._path(ch), len(ch.members))))
        return [info for *_, info in sorted(result, key=lambda r: r[:3])]

    def has_channel(self, channel_id: ChannelId) -> bool:
        return self._voice_channel(channel_id) is not None

    def find_channel(self, name: str) -> ChannelId | None:
        """By id, "Server › Kategorie/Kanal", "Kategorie/Kanal" or just the name; the current server first."""
        wanted = name.strip().casefold()
        current = self.guild.id if self.guild is not None else None
        infos = sorted(self.channels(), key=lambda i: self._voice_channel(i.id).guild.id != current)
        for info in infos:
            local = info.path.split(" › ", 1)[-1].casefold()
            if wanted in (info.id, info.path.casefold(), local, local.rsplit("/", 1)[-1]):
                return info.id
        return None

    def channel_path(self, channel_id: ChannelId | None) -> str:
        ch = self._voice_channel(channel_id)
        return self._path(ch) if ch is not None else ""

    def channel_id_from(self, value: object) -> ChannelId:
        return str(int(str(value)))

    @property
    def moving(self) -> bool:
        return self._moving is not None and not self._moving.done()

    def move_to(self, channel_id: ChannelId) -> None:
        channel = self._voice_channel(channel_id)
        if channel is None or self.moving:
            return
        self._moving = self._spawn(self._move(channel))

    async def _move(self, channel: Any) -> None:
        vc = self.voice_client
        try:
            if self.guild is None or channel.guild.id != self.guild.id:
                # Another server: only one voice channel at a time, so leave the old one first.
                self.guild = channel.guild
                if vc is not None:
                    await vc.disconnect(force=True)
                self.audio.reset()
                vc = channel.guild.voice_client
            if vc is not None and vc.is_connected():
                await vc.move_to(channel, timeout=CONNECT_TIMEOUT)
            else:
                if vc is not None:
                    await vc.disconnect(force=True)
                await channel.connect(self_deaf=True, timeout=CONNECT_TIMEOUT, reconnect=True)
        except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError, OSError) as exc:
            log.warning("Sprachkanal %s: %s", self._path(channel), str(exc) or type(exc).__name__)
            self.board.log_activity("Discord", "system", f"Kanalwechsel nach {self._path(channel)} fehlgeschlagen")
        except Exception:  # noqa: BLE001
            log.exception("Fehler beim Betreten von %s", self._path(channel))
        finally:
            self.board.publish_status()

    # ------------------------------------------------------------------ chat

    async def _send(self, target: Any, html_text: str = "", reference: Any = None,
                    embeds: list[discord.Embed] | None = None) -> None:
        embeds = embeds if embeds is not None else embeds_for(html_text)
        async with self._send_lock:  # FIFO: replies keep their order
            for i, embed in enumerate(embeds):
                try:
                    if i == 0 and reference is not None:
                        await reference.reply(embed=embed, mention_author=False, allowed_mentions=NO_MENTIONS)
                    else:
                        await target.send(embed=embed, allowed_mentions=NO_MENTIONS)
                except discord.HTTPException as exc:
                    log.warning("Nachricht konnte nicht gesendet werden: %s", exc)
                    return

    def announce(self, html_text: str) -> None:
        target = self._announce_channel()
        if target is not None and self.ready:
            self._spawn(self._send(target, html_text))

    def announce_track(self, info: Any, requested_by: str) -> None:
        target = self._announce_channel()
        if target is not None and self.ready:
            self._spawn(self._send(target, embeds=[track_embed(info, requested_by)]))

    def _announce_channel(self) -> Any:
        voice = self._voice_channel(self.channel_id)
        if voice is not None:
            return voice  # the voice channel's own text chat
        for ch in self._text_channels():
            return ch
        return None

    def _text_channels(self) -> list[Any]:
        if self.guild is None:
            return []
        wanted = [w.strip().lstrip("#").casefold() for w in self.cfg.discord_text_channels if w.strip()]
        return [ch for ch in self.guild.text_channels
                if str(ch.id) in wanted or ch.name.casefold() in wanted]

    def accepts_channel(self, channel: Any) -> bool:
        """Chat commands work in configured text channels (default: all) and in voice channel chats."""
        wanted = [w.strip().lstrip("#").casefold() for w in self.cfg.discord_text_channels if w.strip()]
        if not wanted or "*" in wanted:
            return True
        if isinstance(channel, VOICE_TYPES):
            return True
        parent = getattr(channel, "parent", None)  # threads count like their channel
        names = {str(channel.id), getattr(channel, "name", "").casefold()}
        if parent is not None:
            names |= {str(parent.id), parent.name.casefold()}
        return bool(names & set(wanted))

    def is_admin(self, member: Any) -> bool:
        cfg = self.cfg
        if not cfg.admins and not cfg.discord_admin_role:
            return True
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and perms.administrator:
            return True
        role = cfg.discord_admin_role.strip().casefold()
        if role and any(role in (str(r.id), r.name.casefold()) for r in getattr(member, "roles", ())):
            return True
        return bool(cfg.admins) and (cfg.is_admin(member.display_name) or cfg.is_admin(member.name))

    async def _guild_member(self, user: Any) -> Any:
        """The user as a member of one of our servers (for DMs and slash commands), None for strangers."""
        member = self._member(user.id)
        if member is not None:
            return member
        cached = self._member_cache.get(user.id)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        member = None
        for guild in self._guilds_current_first():
            try:
                member = await guild.fetch_member(user.id)
                break
            except discord.HTTPException:
                continue
        self._member_cache[user.id] = (time.monotonic(), member)
        return member

    def command_text(self, message: Any) -> str | None:
        """The command in a message, or None if the message is not for us."""
        prefixes = self.cfg.command_prefixes
        text = message.clean_content.strip()
        me = self.client.user if self.client else None
        private = message.guild is None
        mentioned = me is not None and re.match(rf"<@!?{me.id}>", message.content.strip()) is not None
        if mentioned:  # clean_content shows the leading mention as "@Name"
            member = message.guild.me if message.guild is not None else None
            names = {getattr(member, "display_name", ""), me.name, getattr(me, "global_name", None) or ""}
            for name in sorted((n for n in names if n), key=len, reverse=True):
                if text.startswith(f"@{name}"):
                    text = text[len(name) + 1:].strip()
                    break
        if any(text.startswith(p) for p in prefixes):
            return text
        if (mentioned or private) and text:
            return self.cfg.chat_prefix + text  # "@Soundboard leeroy" and DMs work without a prefix
        return None

    async def on_message(self, message: Any) -> None:
        if self.guild is None or message.author.bot or self.board is None:
            return
        if message.guild is not None:
            if self._guild_by_id(message.guild.id) is None or not self.accepts_channel(message.channel):
                return
            member = message.author
        else:
            member = None
        text = self.command_text(message)
        if text is None:
            return
        if member is None:
            member = await self._guild_member(message.author)
            if member is None:
                return  # DMs only from members of our servers
        private = message.guild is None
        voice = getattr(member, "voice", None)
        channel = message.channel

        def reply(html_text: str) -> Any:
            return self._send(channel, html_text, reference=message)

        async def reply_private(html_text: str) -> None:
            try:
                dm = member.dm_channel or await member.create_dm()
            except discord.HTTPException as exc:
                log.warning("Direktnachricht nicht möglich: %s", exc)
                return
            await self._send(dm, html_text)

        await self.board.on_text(IncomingText(
            sender=member.display_name,
            sender_id=str(member.id),
            text=text,
            reply=reply,
            private=private,
            links=[m.group(1) for m in URL.finditer(message.content)],
            is_admin=self.is_admin(member),
            sender_channel=str(voice.channel.id) if voice is not None and voice.channel is not None else None,
            source="discord",
            account=f"discord:{member.id}",
            reply_private=None if private else reply_private,
        ))

    # ------------------------------------------------------- slash commands

    def _build_commands(self, client: _Client) -> None:
        handler = self.board.commands
        seen: set[str] = set()
        for cmd in handler.commands:
            name = cmd.names[0]
            if name in seen or not SLASH_NAME.match(name) or name != name.lower():
                continue
            seen.add(name)
            has_option, required, option_help = slash_options(cmd.usage)
            description = plain_help(cmd.help, self.cfg.chat_prefix)[:100] or name
            command = app_commands.Command(
                name=name,
                description=description,
                callback=self._make_callback(name, has_option, required),
            )
            if has_option:
                command._params[OPTION].description = option_help
                complete = self._autocomplete_for(name)
                if complete is not None:
                    command.autocomplete(OPTION)(complete)
            client.tree.add_command(command)
        if len(seen) > 100:
            log.warning("Mehr als 100 Slash-Befehle - Discord nimmt nur 100")

    def _make_callback(self, name: str, has_option: bool, required: bool) -> Any:
        platform = self
        if not has_option:
            async def callback(interaction: discord.Interaction) -> None:
                await platform.run_slash(interaction, name, "")
        elif required:
            async def callback(interaction: discord.Interaction, eingabe: str) -> None:
                await platform.run_slash(interaction, name, eingabe)
        else:
            async def callback(interaction: discord.Interaction, eingabe: str | None = None) -> None:
                await platform.run_slash(interaction, name, eingabe or "")
        return callback

    def _autocomplete_for(self, name: str) -> Any:
        board = self.board

        def choices(values: list[str]) -> list[app_commands.Choice[str]]:
            out, seen = [], set()
            for v in values:
                if v and v.casefold() not in seen and len(v) <= 100:
                    seen.add(v.casefold())
                    out.append(app_commands.Choice(name=v, value=v))
                if len(out) == 25:
                    break
            return out

        if name in ("sb", "joinsound"):
            async def sounds(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
                return choices([s.name for s in board.library.search(current, limit=40)])
            return sounds
        if name == "radio":
            async def stations(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
                c = current.casefold()
                return choices([n for n in sorted(board.radio.entries) if c in n])
            return stations
        if name == "unalias":
            async def aliases(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
                c = current.casefold()
                return choices([n for n in sorted(board.aliases.entries) if c in n])
            return aliases
        return None

    async def run_slash(self, interaction: Any, name: str, args: str) -> None:
        if self.guild is None or (interaction.guild_id is not None and self._guild_by_id(interaction.guild_id) is None):
            await interaction.response.send_message("Dieser Bot gehört zu einem anderen Server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        member = interaction.user
        if not hasattr(member, "guild_permissions"):
            member = await self._guild_member(interaction.user) or interaction.user
        answered = False
        lock = asyncio.Lock()

        async def reply(html_text: str) -> None:
            nonlocal answered
            async with lock:
                for embed in embeds_for(html_text):
                    try:
                        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)
                        answered = True
                    except discord.HTTPException as exc:  # e.g. token expired after 15 minutes
                        log.debug("Slash-Antwort verworfen: %s", exc)
                        return

        voice = getattr(member, "voice", None)
        msg = IncomingText(
            sender=getattr(member, "display_name", str(member)),
            sender_id=str(member.id),
            text=f"/{name} {args}".strip(),
            reply=reply,
            private=True,
            links=[m.group(1) for m in URL.finditer(args)],
            is_admin=self.is_admin(member),
            sender_channel=str(voice.channel.id) if voice is not None and voice.channel is not None else None,
            source="discord",
            account=f"discord:{member.id}",
        )
        try:
            await self.board.commands.run(msg, "/", name, args.strip())
        except Exception:  # noqa: BLE001
            log.exception("Fehler bei /%s %s", name, args)
            await reply("Da ist etwas schiefgelaufen.")
        await self.board.commands.flush_replies()
        if not answered:
            try:
                label = f"/{name} {args}".strip().replace("`", "ˋ")
                await interaction.followup.send(embed=discord.Embed(description=f"✓ `{label}`", colour=BRAND),
                                                ephemeral=True)
            except discord.HTTPException:
                pass

    def commands_changed(self) -> None:
        """The command list changed (a collection appeared): update the slash commands."""
        client = self.client
        if client is None:
            return
        client.tree.clear_commands(guild=None)
        self._build_commands(client)
        if client.is_ready():
            for guild in self.guilds:
                self._spawn(self._sync_commands(client, guild))

    async def _sync_commands(self, client: _Client, guild: Any) -> None:
        """Register the slash commands on our server; only when they changed (Discord rate limits this)."""
        target = discord.Object(id=guild.id)
        client.tree.clear_commands(guild=target)
        client.tree.copy_global_to(guild=target)
        payload = [c.to_dict(client.tree) for c in client.tree.get_commands(guild=target)]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        state_file = self.cfg.data_dir / "discord_commands.json"
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get(str(guild.id)) == digest:
            return
        try:
            synced = await client.tree.sync(guild=target)
            self.notices.pop("commands", None)
            state[str(guild.id)] = digest
            state_file.write_text(json.dumps(state), encoding="utf-8")
            log.info("%d Slash-Befehle auf %s registriert", len(synced), guild.name)
        except discord.HTTPException as exc:
            log.warning("Slash-Befehle konnten nicht registriert werden: %s", exc)
            self.notices["commands"] = ("Slash-Befehle konnten nicht registriert werden "
                                        "(Bot mit dem Link aus dem Log neu einladen)")

    # ---------------------------------------------------------------- status

    def status(self) -> dict:
        client = self.client
        user = client.user if client is not None else None
        return {
            "connected": self.ready,
            "server": self.guild.name if self.guild is not None else "Discord",
            "servers": [g.name for g in self.guilds],
            "server_release": "Discord",
            "username": (self.guild.me.display_name if self.guild is not None and self.guild.me is not None
                         else (user.name if user else "Soundboard")),
            "last_error": self.last_error,
            "connected_since": self.connected_since,
            "notice": " · ".join(self.notices.values()),
            "invite_url": self.invite_url,
            "invite_needed": self._invite_needed,
        }
