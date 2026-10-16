"""Mumble adapter: our own asyncio Mumble client behind the platform interface."""

from __future__ import annotations

import html
import logging
import re
from typing import TYPE_CHECKING

import numpy as np

from ..mumble.client import MumbleClient, TextMessage, User
from ..opus import FRAME_SAMPLES, Encoder
from .base import ChannelId, ChannelInfo, IncomingText, PlatformUser

if TYPE_CHECKING:
    from ..config import Config
    from ..core import Soundboard

log = logging.getLogger("mumble")

# Flush the socket when more than this many bytes are queued (slow connection).
MAX_WRITE_BUFFER = 64 * 1024
_HREF = re.compile(r'href\s*=\s*"([^"]+)"', re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<\s*br\s*/?\s*>|</p>", re.IGNORECASE)


def strip_html(message: str) -> str:
    text = _BR.sub(" ", message)
    text = _TAG.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


# A server integration relays chat from elsewhere as a server message: "noah(via Steam): text".
_RELAYED = re.compile(r"^\s*(?P<name>[^():\n]{1,64}?)\s*\(via (?P<via>[^()\n]{1,32})\):\s*(?P<text>\S.*?)\s*$",
                      re.DOTALL)


def parse_relayed(text: str) -> tuple[str, str, str] | None:
    """(name, service, text) of a relayed message, None for other server messages."""
    match = _RELAYED.match(text)
    return (match["name"].strip(), match["via"].strip(), match["text"]) if match else None


def mumble_account(user_id: int | None, name: str) -> str:
    """Registered users keep their ID (safe, survives renames); others are known only by name."""
    if user_id is not None and user_id >= 0:
        return f"mumble:{user_id}"
    return f"mumble-name:{name.casefold()}"


def _user(u: User) -> PlatformUser:
    return PlatformUser(id=str(u.session), name=u.name, channel_id=str(u.channel_id),
                        muted=u.self_mute or u.mute, deaf=u.self_deaf or u.deaf)


class MumbleAudio:
    """Encodes the mixer's frames to Opus and sends them through the TCP tunnel."""

    def __init__(self, client: MumbleClient, bitrate: int) -> None:
        self.client = client
        self.encoder = Encoder(bitrate)
        self._silence = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()

    @property
    def ready(self) -> bool:
        return self.client.ready

    def send_frame(self, pcm: bytes) -> None:
        self.client.send_voice(self.encoder.encode(pcm), terminator=False)

    def end(self) -> None:
        # Explicit end of transmission so clients stop showing the bot as talking.
        if self.client.ready:
            self.client.send_voice(self.encoder.encode(self._silence), terminator=True)

    async def drain(self) -> None:
        if self.client.write_buffer_size > MAX_WRITE_BUFFER:
            await self.client.drain()


class MumblePlatform:
    key = "mumble"
    name = "Mumble"

    def __init__(self, cfg: "Config") -> None:
        self.cfg = cfg
        self.client = MumbleClient(
            host=cfg.mumble_host,
            port=cfg.mumble_port,
            username=cfg.mumble_user,
            password=cfg.mumble_password,
            tokens=cfg.mumble_tokens,
            certfile=str(cfg.cert_file),
            keyfile=str(cfg.key_file),
            listener=self,
        )
        self.audio = MumbleAudio(self.client, cfg.bitrate)
        self.board: "Soundboard | None" = None

    @property
    def message_length(self) -> int:
        return self.client.message_length

    def bind(self, board: "Soundboard") -> None:
        self.board = board

    def message_size(self, html_text: str) -> int:
        return len(html_text)

    async def run_forever(self) -> None:
        await self.client.run_forever()

    async def close(self) -> None:
        await self.client.close()

    # ------------------------------------------------------------------ state

    @property
    def ready(self) -> bool:
        return self.client.ready

    @property
    def me(self) -> PlatformUser | None:
        me = self.client.myself
        return _user(me) if me else None

    @property
    def channel_id(self) -> ChannelId | None:
        me = self.client.myself
        return str(me.channel_id) if me else None

    def users_in_channel(self, channel_id: ChannelId | None) -> list[PlatformUser]:
        if channel_id is None:
            return []
        return [_user(u) for u in self.client.users_in_channel(int(channel_id))]

    def user(self, user_id: str) -> PlatformUser | None:
        u = self.client.users.get(int(user_id))
        return _user(u) if u else None

    def find_user(self, name: str) -> PlatformUser | None:
        u = self.client.find_user(name)
        return _user(u) if u and u.session != self.client.session else None

    def channels(self) -> list[ChannelInfo]:
        c = self.client
        result = [ChannelInfo(str(ch.id), "Root" if ch.id == 0 else (c.channel_path(ch.id) or ch.name),
                              len(c.users_in_channel(ch.id)))
                  for ch in c.channels.values()]
        return sorted(result, key=lambda x: (x.id != "0", x.path.casefold()))

    def has_channel(self, channel_id: ChannelId) -> bool:
        return channel_id.isdigit() and int(channel_id) in self.client.channels

    def find_channel(self, name: str) -> ChannelId | None:
        ch = self.client.find_channel(name)
        return str(ch.id) if ch else None

    def channel_path(self, channel_id: ChannelId | None) -> str:
        if channel_id is None or not channel_id.isdigit():
            return ""
        return self.client.channel_path(int(channel_id))

    def channel_id_from(self, value: object) -> ChannelId:
        return str(int(str(value)))

    @property
    def moving(self) -> bool:
        return self.client.pending_move

    def move_to(self, channel_id: ChannelId) -> None:
        self.client.move_to(int(channel_id))

    def announce(self, html_text: str) -> None:
        me = self.client.myself
        if me is None:
            return
        try:
            self.client.send_text(html_text, channel_id=me.channel_id)
        except ConnectionError:
            pass

    def status(self) -> dict:
        c = self.client
        return {
            "connected": c.ready,
            "server": f"{self.cfg.mumble_host}:{self.cfg.mumble_port}",
            "server_release": f"Mumble {c.server_release}" if c.server_release else "Mumble",
            "username": c.myself.name if c.myself else self.cfg.mumble_user,
            "last_error": c.last_error,
            "connected_since": c.connected_since,
            "bitrate": self.audio.encoder.bitrate,
        }

    # ------------------------------------------------- Mumble client listener

    async def on_ready(self) -> None:
        bitrate = self.client.usable_bitrate(self.cfg.bitrate)
        if bitrate != self.audio.encoder.bitrate:
            self.audio.encoder.set_bitrate(bitrate)
        prefix = self.cfg.command_prefixes[0]
        comment = self.cfg.mumble_comment or (
            f"<b>Soundboard</b><br>Schreib <code>{prefix}help</code> in den Chat für alle Befehle."
        )
        try:
            self.client.set_comment(comment)
        except ConnectionError:
            pass
        await self.board.on_platform_ready(default_channel=self.cfg.mumble_channel,
                                           server=self.cfg.mumble_host)

    async def on_disconnected(self, reason: str) -> None:
        await self.board.on_platform_disconnected(reason)

    async def on_text_message(self, msg: TextMessage) -> None:
        c = self.client
        if msg.actor == c.session:
            return
        me = c.myself
        if msg.is_private:
            if c.session not in msg.sessions:
                return
        elif me is None or not (me.channel_id in msg.channel_ids or msg.tree_ids):
            return
        if msg.actor is None:
            await self._relayed(msg)
            return
        sender = msg.sender or f"Session {msg.actor}"
        actor = c.users.get(msg.actor)

        def reply(html_text: str, private: bool = msg.is_private, actor_id: int = msg.actor) -> None:
            try:
                if private:
                    c.send_text(html_text, session=actor_id)
                elif c.myself is not None:
                    c.send_text(html_text, channel_id=c.myself.channel_id)
            except ConnectionError:
                log.warning("Antwort konnte nicht gesendet werden (nicht verbunden)")

        await self.board.on_text(IncomingText(
            sender=sender,
            sender_id=str(msg.actor),
            text=strip_html(msg.message),
            reply=reply,
            private=msg.is_private,
            links=[html.unescape(h) for h in _HREF.findall(msg.message)],
            is_admin=self.cfg.is_admin(sender),
            sender_channel=str(actor.channel_id) if actor else None,
            source="mumble",
            account=mumble_account(actor.user_id if actor else None, sender),
            reply_private=None if msg.is_private else (lambda text: reply(text, private=True)),
        ))

    async def _relayed(self, msg: TextMessage) -> None:
        """A server message (no sender): only "name(via service): text" from an integration counts.

        Only the server can send these, so a user cannot fake one; the name itself is the integration's
        word, which is why such messages get no admin commands and no /login (see commands.py).
        """
        parsed = parse_relayed(strip_html(msg.message))
        if parsed is None:
            return
        name, via, text = parsed
        c = self.client

        def reply(html_text: str) -> None:
            try:
                if c.myself is not None:
                    c.send_text(html_text, channel_id=c.myself.channel_id)
            except ConnectionError:
                log.warning("Antwort konnte nicht gesendet werden (nicht verbunden)")

        await self.board.on_text(IncomingText(
            sender=name,
            sender_id=f"relay:{via.casefold()}:{name.casefold()}",
            text=text,
            reply=reply,
            links=[html.unescape(h) for h in _HREF.findall(msg.message)],
            is_admin=False,
            source="mumble",
            relayed=True,
        ))

    async def on_user_changed(self, user: User, old: User | None) -> None:
        await self.board.on_user_moved(
            _user(user),
            old_channel=str(old.channel_id) if old is not None else None,
            is_me=user.session == self.client.session,
        )

    async def on_user_removed(self, user: User) -> None:
        self.board.publish_status()

    async def on_permission_denied(self, text: str) -> None:
        self.board.log_activity("Mumble", "system", f"Verweigert: {text}")
