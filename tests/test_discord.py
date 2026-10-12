"""Discord adapter tests with fake discord objects (no network needed)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from app.config import Config
from app.opus import FRAME_SAMPLES
from app.platforms.markdown import html_to_markdown, split_message

try:
    import discord

    from app.platforms import discordbot
except ImportError:  # discord.py is installed in the Docker image; tests elsewhere may lack it
    discord = None
    discordbot = None

needs_discord = unittest.skipIf(discord is None, "discord.py nicht installiert")


def setUpModule():
    # The adapter logs expected failures (bad token, no network) while testing them.
    logging.getLogger("discordbot").setLevel(logging.CRITICAL)


class MarkdownTest(unittest.TestCase):
    def test_formatting(self):
        md = html_to_markdown("&#127925; Jetzt: <b>Song *1*</b> <i>- Zasa</i><br>Mit <code>/sb x`y</code>")
        self.assertEqual(md, "🎵 Jetzt: **Song \\*1\\*** *- Zasa*\nMit `/sb xˋy`")

    def test_links_and_tables(self):
        md = html_to_markdown('<a href="https://youtu.be/x">Titel</a> und <a href="https://a.b">https://a.b</a>')
        self.assertEqual(md, "[Titel](https://youtu.be/x) und https://a.b")
        md = html_to_markdown('<a href="https://de.wikipedia.org/wiki/Anno_(Spiel)">Anno</a>')
        self.assertEqual(md, "[Anno](https://de.wikipedia.org/wiki/Anno_%28Spiel%29)")
        md = html_to_markdown("<table><tr><td>1</td><td>A</td></tr><tr><td>2</td><td>B</td></tr></table>")
        self.assertEqual(md, "1 – A\n2 – B")

    def test_line_start_and_table_title(self):
        self.assertEqual(html_to_markdown("# nein<br>&gt; zitat<br>a &gt; b"), "\\# nein\n\\> zitat\na > b")
        self.assertEqual(html_to_markdown("<b>Hilfe</b><table><tr><td><code>!sb &lt;name&gt;</code></td>"
                                          "<td>spielt</td></tr></table>"), "**Hilfe**\n`!sb <name>` – spielt")

    def test_empty_bold_and_whitespace(self):
        self.assertEqual(html_to_markdown("<b></b>  a  <br><br><br><br>b"), "a\n\nb")

    def test_split(self):
        text = "\n".join(f"Zeile {i:04d}" for i in range(600))
        parts = split_message(text, 2000)
        self.assertTrue(all(len(p) <= 2000 for p in parts))
        self.assertEqual("\n".join(parts), text)
        self.assertEqual(split_message("x" * 4500, 2000), ["x" * 2000, "x" * 2000, "x" * 500])
        self.assertEqual(split_message(""), [])


def frame(value: int = 1000) -> bytes:
    return np.full(FRAME_SAMPLES, value, dtype=np.int16).tobytes()


@needs_discord
class PcmSourceTest(unittest.TestCase):
    def test_read_semantics(self):
        src = discordbot.PcmSource()
        src.push(discordbot.to_stereo(frame(5)))
        data = src.read()
        self.assertEqual(len(data), discordbot.STEREO_FRAME_BYTES)
        self.assertTrue(np.all(np.frombuffer(data, dtype=np.int16) == 5))
        # Mixer late: silence instead of ending the player.
        self.assertEqual(src.read(), bytes(discordbot.STEREO_FRAME_BYTES))
        src.push(discordbot.to_stereo(frame()))
        src.end()
        self.assertEqual(len(src.read()), discordbot.STEREO_FRAME_BYTES)  # queued audio still plays
        self.assertEqual(src.read(), b"")
        self.assertTrue(src.finished)
        self.assertFalse(src.push(discordbot.to_stereo(frame())))
        self.assertFalse(src.is_opus())

    def test_end_then_more_audio_continues(self):
        src = discordbot.PcmSource()
        src.push(b"a" * 3840)
        src.end()
        self.assertTrue(src.push(b"b" * 3840))  # next burst before the player noticed the end
        src.read()
        self.assertEqual(src.read(), b"b" * 3840)
        self.assertNotEqual(src.read(), b"")

    def test_bounded_latency(self):
        src = discordbot.PcmSource()
        for i in range(discordbot.MAX_QUEUED_FRAMES + 10):
            src.push(bytes([i % 256]) * 3840)
        self.assertEqual(src.queued, discordbot.MAX_QUEUED_FRAMES)
        self.assertEqual(src.dropped, 10)
        self.assertEqual(src.read()[0], 10)

    def test_discord_encoder_accepts_frames(self):
        discordbot.ensure_opus()
        enc = discord.opus.Encoder(bitrate=64)
        data = enc.encode(discordbot.to_stereo(frame()), enc.SAMPLES_PER_FRAME)
        self.assertGreater(len(data), 3)


class FakeVoiceClient:
    def __init__(self):
        self.connected = True
        self.player = None
        self.plays = 0
        self.fail_next = False
        self.channel = SimpleNamespace(bitrate=96000)

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.player is not None and not self.player.finished

    def play(self, source, *, after=None, **kwargs):
        if self.fail_next:
            self.fail_next = False
            raise discord.ClientException("Already playing audio.")
        if self.is_playing():
            raise discord.ClientException("Already playing audio.")
        self.player, self.after, self.kwargs = source, after, kwargs
        self.plays += 1


@needs_discord
class DiscordAudioTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.vc = FakeVoiceClient()
        self.platform = SimpleNamespace(voice_client=self.vc, bitrate_kbps=lambda: 96)
        self.audio = discordbot.DiscordAudio(self.platform)

    async def test_prebuffer_and_single_player(self):
        for _ in range(discordbot.PREBUFFER_FRAMES - 1):
            self.audio.send_frame(frame())
        self.assertEqual(self.vc.plays, 0)
        self.audio.send_frame(frame())
        self.assertEqual(self.vc.plays, 1)
        self.assertEqual(self.vc.kwargs["bitrate"], 96)
        for _ in range(10):
            self.audio.send_frame(frame())
        self.assertEqual(self.vc.plays, 1)

    async def test_short_burst_plays_on_end(self):
        self.audio.send_frame(frame())
        self.audio.end()
        self.assertEqual(self.vc.plays, 1)
        src = self.vc.player
        self.assertNotEqual(src.read(), b"")
        self.assertEqual(src.read(), b"")

    async def test_new_burst_after_player_finished(self):
        for _ in range(3):
            self.audio.send_frame(frame())
        self.audio.end()
        first = self.vc.player
        while first.read():
            pass
        # The previous player is still shutting down: play() fails once, the next frame retries.
        self.vc.fail_next = True
        for _ in range(3):
            self.audio.send_frame(frame())
        self.assertEqual(self.vc.plays, 1)
        self.audio.send_frame(frame())
        self.assertEqual(self.vc.plays, 2)
        self.assertIsNot(self.vc.player, first)

    async def test_after_callback_starts_pending_burst(self):
        self.audio.send_frame(frame())
        first = self.vc.player = discordbot.PcmSource()  # someone else's player still running
        self.audio.end()
        self.assertIs(self.vc.player, first)
        first.cleanup()
        self.audio._after(None)
        await asyncio.sleep(0)
        self.assertEqual(self.vc.plays, 1)
        self.assertIsNot(self.vc.player, first)

    async def test_not_connected(self):
        self.vc.connected = False
        self.assertFalse(self.audio.ready)
        self.audio.send_frame(frame())
        self.assertEqual(self.vc.plays, 0)


# ------------------------------------------------------------- fake guild


class FakeVoice:
    def __init__(self, cid, name, category=None, position=0):
        self.id, self.name, self.category, self.position = cid, name, category, position
        self.members = []
        self.bitrate = 64000


class FakeText:
    def __init__(self, cid, name):
        self.id, self.name = cid, name
        self.sent = []

    async def send(self, text, **kwargs):
        self.sent.append(text)


def member(mid, name, *, channel=None, bot=False, admin=False, roles=()):
    voice = SimpleNamespace(channel=channel, self_mute=False, mute=False, self_deaf=False, deaf=False) if channel else None
    m = SimpleNamespace(id=mid, name=name.lower(), display_name=name, global_name=name, bot=bot, voice=voice,
                        guild_permissions=SimpleNamespace(administrator=admin), roles=list(roles))
    if channel is not None:
        channel.members.append(m)
    return m


class FakeGuild:
    def __init__(self):
        self.id = 42
        self.name = "Gilde"
        games = SimpleNamespace(name="Spiele", position=1)
        self.lobby = FakeVoice(100, "Lobby", position=0)
        self.dota = FakeVoice(12345678901234567890, "Dota", games, position=1)
        self.general = FakeText(200, "allgemein")
        self.bots = FakeText(201, "bot-befehle")
        self.text_channels = [self.general, self.bots]
        self.channels = [self.dota, self.lobby, self.general, self.bots]
        self.me = member(1, "Soundboard", bot=True)
        self.zasa = member(2, "Zasa", channel=self.dota, roles=[SimpleNamespace(id=7, name="DJ")])
        self.noah = member(3, "Noah")
        self.members = [self.me, self.zasa, self.noah]
        self.voice_client = None

    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)

    def get_member(self, mid):
        return next((m for m in self.members if m.id == mid), None)

    async def fetch_member(self, mid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "unknown member")


def make_board(tmp: Path, **cfg_changes):
    from app.core import Soundboard

    (tmp / "sounds").mkdir()
    (tmp / "sounds" / "leeroy.wav").write_bytes(b"")
    cfg = Config(platform="discord", discord_token="x", data_dir=tmp / "data", sounds_dir=tmp / "sounds",
                 collections_dirs=(), tts_model=tmp / "none.onnx", command_prefixes=["!"], **cfg_changes)
    board = Soundboard(cfg)
    board.library.scan()
    return board


@needs_discord
class DiscordPlatformTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(discordbot, "VOICE_TYPES", (FakeVoice,))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.board = make_board(Path(self._tmp.name))
        self.platform = self.board.platform
        self.guild = FakeGuild()
        self.platform.guild = self.guild
        self.platform.client = SimpleNamespace(user=SimpleNamespace(id=1, name="soundboard", global_name=None),
                                               is_ready=lambda: True)
        self.texts = []
        self.board.on_text = mock.AsyncMock(side_effect=lambda msg: self.texts.append(msg))

    def configure(self, **changes):
        self.board.cfg = self.platform.cfg = dataclasses.replace(self.board.cfg, **changes)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def message(self, content, *, author=None, channel=None, guild=True, clean=None):
        return SimpleNamespace(content=content, clean_content=clean if clean is not None else content,
                               author=author or self.guild.zasa, channel=channel or self.guild.general,
                               guild=self.guild if guild else None)

    async def test_channels_and_users(self):
        infos = self.platform.channels()
        self.assertEqual([(i.id, i.path, i.users) for i in infos],
                         [("100", "Lobby", 0), ("12345678901234567890", "Spiele/Dota", 1)])
        self.assertEqual(self.platform.find_channel("dota"), "12345678901234567890")
        self.assertEqual(self.platform.find_channel("Spiele/Dota"), "12345678901234567890")
        self.assertEqual(self.platform.channel_id_from("12345678901234567890"), "12345678901234567890")
        self.assertTrue(self.platform.has_channel("100"))
        self.assertFalse(self.platform.has_channel("200"))  # a text channel
        self.assertEqual(self.platform.find_user("@zasa").name, "Zasa")
        self.assertEqual(self.platform.find_user("<@3>").name, "Noah")
        self.assertIsNone(self.platform.find_user("Soundboard"))  # never the bot itself
        self.assertEqual([u.name for u in self.platform.users_in_channel("12345678901234567890")], ["Zasa"])
        self.assertIsNone(self.platform.channel_id)

    async def test_chat_command(self):
        await self.platform.on_message(self.message("!sb leeroy https://x.y/z", clean="!sb leeroy https://x.y/z"))
        msg = self.texts[-1]
        self.assertEqual((msg.sender, msg.text, msg.private), ("Zasa", "!sb leeroy https://x.y/z", False))
        self.assertEqual(msg.links, ["https://x.y/z"])
        self.assertEqual(msg.sender_channel, "12345678901234567890")
        self.assertTrue(msg.is_admin)  # no admins configured: everybody
        self.assertEqual(msg.source, "discord")

    async def test_ignores_other_messages(self):
        await self.platform.on_message(self.message("hallo"))
        await self.platform.on_message(self.message("!sb x", author=self.guild.me))
        other = SimpleNamespace(id=99)
        await self.platform.on_message(SimpleNamespace(content="!sb x", clean_content="!sb x", author=self.guild.zasa,
                                                       channel=self.guild.general, guild=other))
        self.assertEqual(self.texts, [])

    async def test_mention_and_dm_without_prefix(self):
        await self.platform.on_message(self.message("<@1> leeroy", clean="@Soundboard leeroy"))
        self.assertEqual(self.texts[-1].text, "!leeroy")
        await self.platform.on_message(self.message("np", guild=False))
        self.assertEqual((self.texts[-1].text, self.texts[-1].private), ("!np", True))
        stranger = member(77, "Fremd")
        await self.platform.on_message(self.message("np", author=stranger, guild=False))
        self.assertEqual(len(self.texts), 2)  # DMs only from members of our server

    async def test_text_channel_filter(self):
        self.configure(discord_text_channels=["#bot-befehle"])
        await self.platform.on_message(self.message("!np"))
        self.assertEqual(self.texts, [])
        await self.platform.on_message(self.message("!np", channel=self.guild.bots))
        await self.platform.on_message(self.message("!np", channel=self.guild.dota))  # voice channel chat
        self.assertEqual(len(self.texts), 2)

    async def test_admins(self):
        self.configure(discord_admin_role="dj")
        self.assertTrue(self.platform.is_admin(self.guild.zasa))
        self.assertFalse(self.platform.is_admin(self.guild.noah))
        self.configure(discord_admin_role="", admins=["Noah"])
        self.assertTrue(self.platform.is_admin(self.guild.noah))
        self.assertFalse(self.platform.is_admin(self.guild.zasa))
        self.guild.zasa.guild_permissions.administrator = True
        self.assertTrue(self.platform.is_admin(self.guild.zasa))

    async def test_reply_is_markdown_in_order(self):
        sent = []

        class Ref:
            async def reply(self, *, embed, **kwargs):
                await asyncio.sleep(0.01)
                sent.append(("reply", embed.description, embed.colour))

        class Chan:
            async def send(self, *, embed, **kwargs):
                sent.append(("send", embed.description, embed.colour))

        tasks = [asyncio.ensure_future(self.platform._send(Chan(), "<b>eins</b>", reference=Ref())),
                 asyncio.ensure_future(self.platform._send(Chan(), "zwei")),
                 asyncio.ensure_future(self.platform._send(Chan(), ""))]
        await asyncio.gather(*tasks)
        brand = discordbot.BRAND
        self.assertEqual(sent, [("reply", "**eins**", brand), ("send", "zwei", brand)])

    def test_long_reply_is_split_into_embeds(self):
        rows = "".join(f"<tr><td>Zeile {i}</td><td>{'x' * 80}</td></tr>" for i in range(100))
        embeds = discordbot.embeds_for(f"<b>Liste</b><table>{rows}</table>")
        self.assertGreater(len(embeds), 1)
        self.assertTrue(all(len(e.description) <= 4096 for e in embeds))
        self.assertTrue(embeds[0].description.startswith("**Liste**\nZeile 0 – "))

    def test_now_playing_card(self):
        from app.media import TrackInfo

        live = discordbot.track_embed(TrackInfo(source="x", title="lofi hip hop radio 📚 beats to relax/study to",
                                                webpage="https://www.youtube.com/watch?v=rFZHOHl-L8A",
                                                thumbnail="https://i.ytimg.com/vi/x/hq.jpg", uploader="Lofi_Girl",
                                                is_live=True), "Web")
        self.assertEqual((live.author.name, live.title, live.url),
                         ("🎵 Jetzt läuft", "lofi hip hop radio 📚 beats to relax/study to",
                          "https://www.youtube.com/watch?v=rFZHOHl-L8A"))
        self.assertEqual(live.description, "🔴 **LIVE** · Lofi\\_Girl")
        self.assertEqual((live.colour, live.footer.text, live.thumbnail.url),
                         (discordbot.LIVE, "gewünscht von Web", "https://i.ytimg.com/vi/x/hq.jpg"))
        song = discordbot.track_embed(TrackInfo(source="x", title="Song", webpage="ytsearch:x", duration=3725), "Zasa")
        self.assertEqual((song.url, song.description, song.thumbnail.url), (None, "⏱ 1:02:05", None))

    async def test_voice_state_update(self):
        moved = mock.AsyncMock()
        self.board.on_user_moved = moved
        before = SimpleNamespace(channel=self.guild.lobby)
        after = SimpleNamespace(channel=self.guild.dota)
        await self.platform.on_voice_state_update(SimpleNamespace(**vars(self.guild.zasa), guild=self.guild),
                                                  before, after)
        user = moved.call_args.args[0]
        self.assertEqual((user.name, user.channel_id), ("Zasa", "12345678901234567890"))
        self.assertEqual(moved.call_args.kwargs, {"old_channel": "100", "is_me": False})

    async def test_join_and_move(self):
        self.guild.lobby.connect = mock.AsyncMock()
        self.platform.move_to("100")
        self.assertTrue(self.platform.moving)
        await self.platform._moving
        self.assertFalse(self.platform.moving)
        self.guild.lobby.connect.assert_awaited_once_with(self_deaf=True, timeout=discordbot.CONNECT_TIMEOUT,
                                                          reconnect=True)
        vc = self.guild.voice_client = SimpleNamespace(is_connected=lambda: True, move_to=mock.AsyncMock())
        self.platform.move_to("12345678901234567890")
        await self.platform._moving
        vc.move_to.assert_awaited_once_with(self.guild.dota, timeout=discordbot.CONNECT_TIMEOUT)
        self.platform.move_to("200")  # not a voice channel
        self.assertFalse(self.platform.moving)

    async def test_join_failure_is_logged(self):
        self.guild.lobby.connect = mock.AsyncMock(side_effect=asyncio.TimeoutError())
        self.platform.move_to("100")
        await self.platform._moving
        self.assertIn("fehlgeschlagen", self.board.activity[-1].text)

    async def test_slash_commands_are_valid(self):
        client = discordbot._Client(self.platform, discord.Intents.default())
        self.platform._build_commands(client)
        commands = client.tree.get_commands()
        names = [c.name for c in commands]
        self.assertIn("sb", names)
        self.assertIn("help", names)
        self.assertLessEqual(len(commands), 100)
        for cmd in commands:
            data = cmd.to_dict(client.tree)
            self.assertRegex(data["name"], r"^[-_a-z0-9]{1,32}$")
            self.assertTrue(1 <= len(data["description"]) <= 100, data)
            self.assertNotRegex(data["description"], r"&\w+;|</?(code|b|i|br|a)\b")  # no HTML left
        sb = client.tree.get_command("sb").to_dict(client.tree)
        self.assertEqual(sb["options"][0]["name"], "eingabe")
        self.assertTrue(sb["options"][0]["required"])
        self.assertTrue(sb["options"][0]["autocomplete"])
        self.assertFalse(client.tree.get_command("random").to_dict(client.tree)["options"][0]["required"])
        self.assertEqual(client.tree.get_command("stop").to_dict(client.tree)["options"], [])
        await client.close()

    async def test_slash_commands_rebuilt_when_collections_change(self):
        client = discordbot._Client(self.platform, discord.Intents.default())
        self.platform.client = client
        self.platform._build_commands(client)
        before = {c.name for c in client.tree.get_commands()}
        self.platform.commands_changed()  # no "already registered" error, same set again
        self.assertEqual({c.name for c in client.tree.get_commands()}, before)
        await client.close()

    async def test_autocomplete(self):
        complete = self.platform._autocomplete_for("sb")
        choices = await complete(None, "lee")
        self.assertEqual([c.value for c in choices], ["leeroy"])

    async def test_slash_run(self):
        followups = []
        interaction = SimpleNamespace(
            guild_id=42, user=self.guild.zasa,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock(
                side_effect=lambda *, embed, **kw: followups.append(embed.description))),
        )
        await self.platform.run_slash(interaction, "help", "")
        interaction.response.defer.assert_awaited_once()
        self.assertTrue(followups and "**" in followups[0], followups[:1])
        followups.clear()
        await self.platform.run_slash(interaction, "follow", "Noah")
        self.assertEqual(followups, ["Ich folge jetzt **Noah**."])
        self.assertEqual(self.board.follow_user, "Noah")
        followups.clear()
        with mock.patch.object(self.board.commands, "run", mock.AsyncMock()):  # a command without reply
            await self.platform.run_slash(interaction, "stop", "")
        self.assertEqual(followups, ["✓ `/stop`"])

    async def test_login_failure_keeps_running(self):
        with mock.patch.object(discordbot._Client, "start", side_effect=discord.LoginFailure("bad")):
            await asyncio.wait_for(self.platform.run_forever(), 5)
        self.assertIn("Token", self.platform.status()["last_error"])

    async def test_network_error_retries(self):
        import aiohttp

        attempts = []

        async def start(client, token, reconnect=True):
            attempts.append(1)
            if len(attempts) == 1:
                raise aiohttp.ClientConnectionError("dns")
            raise discord.LoginFailure("stop")

        with mock.patch.object(discordbot._Client, "start", start), \
                mock.patch("asyncio.sleep", mock.AsyncMock()) as sleep:
            await asyncio.wait_for(self.platform.run_forever(), 5)
        self.assertEqual(len(attempts), 2)
        sleep.assert_awaited_once_with(5.0)
        self.assertEqual(discordbot.describe_error(aiohttp.ClientConnectionError("x")),
                         "Discord ist nicht erreichbar (Internetverbindung?)")

    async def test_missing_intent_falls_back(self):
        calls = []

        async def start(client, token, reconnect=True):
            calls.append(client.intents.message_content)
            if len(calls) == 1:
                raise discord.PrivilegedIntentsRequired(None)
            raise discord.LoginFailure("stop here")

        with mock.patch.object(discordbot._Client, "start", start):
            await asyncio.wait_for(self.platform.run_forever(), 5)
        self.assertEqual(calls, [True, False])
        self.assertIn("Message Content", self.platform.status()["notice"])


class PlayerVoiceClient:
    """A voice client that runs discord.py's real AudioPlayer thread and records what it would send."""

    def __init__(self, loop):
        self.client = SimpleNamespace(loop=loop)
        self.speaking = []
        self.ws = SimpleNamespace(speak=self._speak)
        self.frames = []
        self.packets = 0
        self.channel = SimpleNamespace(bitrate=64000)
        self._player = None
        self.encoder = None

    async def _speak(self, state):
        self.speaking.append(int(state))

    def is_connected(self):
        return True

    def is_playing(self):
        return self._player is not None and self._player.is_playing()

    def play(self, source, *, after=None, **encoder_args):
        from discord.player import AudioPlayer

        if self.is_playing():
            raise discord.ClientException("Already playing audio.")
        self.encoder = discord.opus.Encoder(**encoder_args)
        self._player = AudioPlayer(source, self, after=after)
        self._player.start()

    def send_audio_packet(self, data, *, encode=True):
        if encode:
            self.frames.append(data)
            self.encoder.encode(data, self.encoder.SAMPLES_PER_FRAME)
        self.packets += 1


@needs_discord
class DiscordPipelineTest(unittest.IsolatedAsyncioTestCase):
    """Soundboard -> mixer -> DiscordAudio -> discord.py AudioPlayer, in real time."""

    async def test_two_sounds_play_through_discord_player(self):
        import subprocess

        discordbot.ensure_opus()
        with tempfile.TemporaryDirectory() as tmp:
            board = make_board(Path(tmp))
            for name, freq in (("ton", 440), ("piep", 880)):
                subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                                f"sine=frequency={freq}:duration=0.4", str(Path(tmp) / "sounds" / f"{name}.wav")],
                               check=True)
            board.library.scan()
            board.events.bind(asyncio.get_running_loop())
            vc = PlayerVoiceClient(asyncio.get_running_loop())
            guild = FakeGuild()
            guild.voice_client = vc
            board.platform.guild = guild
            board.platform.client = SimpleNamespace(user=None, is_ready=lambda: True)
            board.mixer.start()
            try:
                sounds = {s.name: s for s in board.library.sounds.values()}
                await board.play(sounds["ton"], requested_by="Zasa", source="discord")
                await asyncio.sleep(0.9)
                first = len(vc.frames)
                await board.play(sounds["piep"], requested_by="Zasa", source="discord")
                await asyncio.sleep(0.9)
            finally:
                await board.mixer.close()
            self.assertFalse(vc.is_playing())
            # Two bursts of ~20 frames (0.4 s), each played by its own player, ended cleanly.
            self.assertTrue(17 <= first <= 26, first)
            self.assertTrue(17 <= len(vc.frames) - first <= 26, len(vc.frames) - first)
            self.assertEqual(vc.speaking.count(1), 2)
            self.assertEqual(vc.speaking[-1], 0)
            pcm = np.frombuffer(b"".join(vc.frames), dtype=np.int16).reshape(-1, 2)
            self.assertTrue(np.array_equal(pcm[:, 0], pcm[:, 1]))  # mono duplicated to stereo
            self.assertGreater(int(np.abs(pcm).max()), 1000)
            silent = sum(1 for f in vc.frames if not any(f))
            self.assertLessEqual(silent, 2)  # no gaps from the mixer/player hand-over


if __name__ == "__main__":
    unittest.main()
