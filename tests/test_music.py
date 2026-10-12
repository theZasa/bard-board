"""Tests for the music queue with a fake resolver and real ffmpeg decoding of local files."""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.media import Lookup, MediaError, Stream, TrackInfo, format_time, parse_time
from app.music import MusicPlayer
from app.opus import SAMPLE_RATE


def tone(path: Path, freq: int, seconds: float) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    str(path)], check=True)


class FakeResolver:
    """Resolves keys to local files. `blocked` keys get an unreachable stream URL (like a 403 from
    googlevideo for ffmpeg), only the downloader command (cat) can read them."""

    def __init__(self, files: dict[str, tuple[Path, float | None]], blocked: set[str] | None = None) -> None:
        self.files = files
        self.blocked = blocked or set()
        self.live: set[str] = set()  # keys whose stream turns out to be live only when it is opened
        self.stream_calls = 0
        self.download_calls = 0

    def download_command(self, source: str) -> list[str]:
        self.download_calls += 1
        return ["cat", str(self.files[source][0])]

    async def lookup(self, query: str) -> Lookup:
        if query == "playlist":
            return Lookup([self._info(k) for k in ("a", "b", "long", "live", "upcoming")], "Meine Liste")
        if query not in self.files:
            raise MediaError("Nichts gefunden")
        return Lookup([self._info(query)])

    def _info(self, key: str) -> TrackInfo:
        _, duration = self.files[key]
        return TrackInfo(source=key, title=f"Titel {key}", webpage=f"https://example.org/{key}", duration=duration,
                         is_live=key == "live", starts_at=1893456000.0 if key == "upcoming" else None)

    async def stream(self, source: str) -> Stream:
        self.stream_calls += 1
        path, duration = self.files[source]
        if source in self.blocked:
            return Stream(url="http://127.0.0.1:9/blocked.mp3", duration=duration, expires_at=1e12)
        return Stream(url=str(path), duration=duration, expires_at=1e12, is_live=source in self.live)


def dominant(pcm: np.ndarray) -> float:
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float32) * np.hanning(pcm.size)))
    return float(np.fft.rfftfreq(pcm.size, 1 / SAMPLE_RATE)[np.argmax(spectrum)])


class MusicPlayerTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        tone(root / "a.mp3", 300, 3)
        tone(root / "b.ogg", 600, 3)
        tone(root / "c.mp3", 900, 8)
        cls.files = {
            "a": (root / "a.mp3", 3.0),
            "b": (root / "b.ogg", 3.0),
            "c": (root / "c.mp3", 8.0),
            "long": (root / "a.mp3", 7200.0),
            "live": (root / "a.mp3", None),
            "upcoming": (root / "a.mp3", None),
        }
        cls.root = root

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def make(self, state_name: str = "music.json", blocked: set[str] | None = None
             ) -> tuple[MusicPlayer, FakeResolver, list[str], list[str]]:
        resolver = FakeResolver(self.files, blocked)
        notices: list[str] = []
        started: list[str] = []
        player = MusicPlayer(
            resolver,  # type: ignore[arg-type]
            state_file=self.root / state_name,
            volume=80,
            max_seconds=20 * 60,
            normalize=False,
            on_change=lambda: None,
            on_notice=notices.append,
            on_active=lambda: None,
            on_track=lambda t: started.append(t.info.title),
        )
        return player, resolver, notices, started

    async def drain(self, player: MusicPlayer, seconds: float, *, until_idle: bool = False, speed: float = 10) -> np.ndarray:
        """Pull frames like the mixer does, `speed` times faster than real time."""
        frames = []
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        started = player.state != "idle"
        while loop.time() < end:
            started = started or player.state != "idle"
            if player.active:
                chunk = player.pull_frame()
                if chunk is not None:
                    frames.append(chunk.copy())
                    await asyncio.sleep(0.02 / speed)
                    continue
            if until_idle and started and player.state == "idle" and not player.queue:
                break
            await asyncio.sleep(0.005)
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)

    async def test_plays_queue_in_order_and_announces(self):
        player, _, notices, started = self.make("order.json")
        player.start()
        first = await player.add("a", requested_by="Zasa", source="mumble")
        second = await player.add("b", requested_by="Noah", source="web")
        self.assertEqual((first.position, second.position), (0, 1))
        pcm = await self.drain(player, 15, until_idle=True)
        await player.close()
        self.assertEqual(started, ["Titel a", "Titel b"])
        self.assertEqual(notices, [])
        half = pcm.size // 2
        self.assertAlmostEqual(dominant(pcm[: half - SAMPLE_RATE // 2]), 300, delta=10)
        self.assertAlmostEqual(dominant(pcm[half + SAMPLE_RATE // 2:]), 600, delta=10)
        self.assertAlmostEqual(pcm.size / SAMPLE_RATE, 6.0, delta=0.3)

    async def test_playlist_skips_long_and_upcoming_but_takes_live(self):
        player, *_ = self.make("playlist.json")
        result = await player.add("playlist", requested_by="Zasa", source="mumble")
        self.assertEqual([t.info.title for t in result.tracks], ["Titel a", "Titel b", "Titel live"])
        self.assertEqual((result.playlist_title, result.skipped), ("Meine Liste", 2))
        with self.assertRaises(MediaError) as ctx:
            await player.add("long", requested_by="Zasa", source="mumble")
        self.assertIn("zu lang", str(ctx.exception))
        with self.assertRaises(MediaError) as ctx:
            await player.add("upcoming", requested_by="Zasa", source="mumble")
        self.assertIn("startet erst am 01.01.", str(ctx.exception))
        live = await player.add("live", requested_by="Zasa", source="mumble")
        self.assertEqual(live.tracks[0].label(), "Titel live (LIVE)")

    async def test_queue_management_and_persistence(self):
        player, *_ = self.make("persist.json")
        for key in ("a", "b", "c"):
            await player.add(key, requested_by="Zasa", source="web")
        ids = [t.id for t in player.queue]
        self.assertTrue(player.move(ids[2], 0))
        self.assertEqual([t.info.title for t in player.queue], ["Titel c", "Titel a", "Titel b"])
        self.assertIsNotNone(player.remove_at(2))
        await player.add("a", requested_by="Pia", source="web", play_next=True)
        self.assertEqual([t.info.title for t in player.queue], ["Titel a", "Titel c", "Titel b"])
        player.set_volume(35)
        saved = json.loads((self.root / "persist.json").read_text())
        self.assertEqual([q["title"] for q in saved["queue"]], ["Titel a", "Titel c", "Titel b"])

        restored, *_ = self.make("persist.json")
        self.assertEqual([t.info.title for t in restored.queue], ["Titel a", "Titel c", "Titel b"])
        self.assertEqual((restored.volume, restored.paused, restored.state), (35, True, "paused"))
        self.assertEqual(restored.clear(), 3)

    async def test_resumes_after_decoder_crash(self):
        player, resolver, notices, started = self.make("crash.json")
        player.start()
        await player.add("c", requested_by="Zasa", source="mumble")
        first = await self.drain(player, 1.0, speed=2)
        self.assertGreater(first.size, SAMPLE_RATE)
        position = player.position
        player._decoder.kill()  # simulate a dropped connection
        rest = await self.drain(player, 20, until_idle=True)
        await player.close()
        total = (first.size + rest.size) / SAMPLE_RATE
        self.assertEqual(notices, [])
        self.assertEqual(started, ["Titel c"], "a restart must not announce the title again")
        self.assertGreaterEqual(resolver.stream_calls, 2, "stream must be resolved again")
        self.assertAlmostEqual(total, 8.0, delta=0.8, msg=f"played {total:.1f}s, crash at {position:.1f}s")
        self.assertAlmostEqual(dominant(rest), 900, delta=10)

    async def test_shutdown_keeps_current_title_and_position(self):
        player, *_ = self.make("shutdown.json")
        player.start()
        await player.add("c", requested_by="Zasa", source="mumble")
        await player.add("a", requested_by="Zasa", source="mumble")
        await self.drain(player, 1.0, speed=2)
        position = player.position
        await player.close()
        restored, *_ = self.make("shutdown.json")
        self.assertEqual([t.info.title for t in restored.queue], ["Titel c", "Titel a"])
        self.assertAlmostEqual(restored.queue[0].start_at, position, delta=0.1)
        self.assertTrue(restored.paused)

    async def test_skip_pause_resume(self):
        player, _, _, started = self.make("skip.json")
        player.start()
        await player.add("c", requested_by="Zasa", source="mumble")
        await player.add("a", requested_by="Zasa", source="mumble")
        await self.drain(player, 1.0, speed=2)
        self.assertTrue(player.pause())
        self.assertFalse(player.active)
        position = player.position
        await asyncio.sleep(0.3)
        self.assertEqual(player.position, position)
        self.assertTrue(player.resume())
        player.skip()
        pcm = await self.drain(player, 10, until_idle=True)
        await player.close()
        self.assertEqual(started, ["Titel c", "Titel a"])
        self.assertAlmostEqual(dominant(pcm[-SAMPLE_RATE:]), 300, delta=10)

    async def test_falls_back_to_downloader_when_stream_url_is_rejected(self):
        player, resolver, notices, started = self.make("fallback.json", blocked={"a"})
        player.start()
        await player.add("a", requested_by="Zasa", source="mumble")
        pcm = await self.drain(player, 15, until_idle=True)
        await player.close()
        self.assertEqual(notices, [])
        self.assertEqual(started, ["Titel a"])
        self.assertEqual((resolver.stream_calls, resolver.download_calls), (2, 1), "fresh URL first, then downloader")
        self.assertAlmostEqual(pcm.size / SAMPLE_RATE, 3.0, delta=0.3)
        self.assertAlmostEqual(dominant(pcm), 300, delta=10)

    async def test_resume_in_downloader_mode_seeks_in_pipe(self):
        player, resolver, notices, _ = self.make("fallback-resume.json", blocked={"c"})
        player.start()
        await player.add("c", requested_by="Zasa", source="mumble")
        parts = []
        for _ in range(40):  # the fallback ladder takes a moment, play ~2 s once it runs
            parts.append(await self.drain(player, 0.25, speed=2))
            if player.position >= 2.0:
                break
        first = np.concatenate(parts)
        self.assertGreaterEqual(player.position, 2.0)
        player._decoder.kill()  # connection drop while using the downloader
        rest = await self.drain(player, 20, until_idle=True)
        await player.close()
        total = (first.size + rest.size) / SAMPLE_RATE
        self.assertEqual(notices, [])
        self.assertEqual(resolver.download_calls, 2, "restart must stay with the downloader")
        self.assertAlmostEqual(total, 8.0, delta=0.8)

    async def test_broken_track_reports_and_continues(self):
        broken = self.root / "broken.mp3"
        broken.write_bytes(b"kein audio" * 50)
        self.files["broken"] = (broken, 3.0)
        player, resolver, notices, started = self.make("broken.json")
        player.start()
        await player.add("broken", requested_by="Zasa", source="mumble")
        await player.add("a", requested_by="Zasa", source="mumble")
        await self.drain(player, 10, until_idle=True)
        await player.close()
        self.assertEqual(len(notices), 1)
        self.assertIn("Titel broken", notices[0])
        self.assertEqual(started, ["Titel a"])
        self.assertEqual(resolver.download_calls, 0, "corrupt media is not retried")


class LivestreamTest(unittest.IsolatedAsyncioTestCase):
    """A real HLS livestream written by ffmpeg in real time, like YouTube serves them."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "live").mkdir()
        self.playlist = self.root / "live" / "index.m3u8"
        self.generator = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "sine=frequency=500", "-c:a", "aac", "-f", "hls",
             "-hls_time", "1", "-hls_list_size", "6", "-hls_flags", "delete_segments", str(self.playlist)],
            stdin=subprocess.DEVNULL,
        )
        for _ in range(100):  # wait for the first segments
            if self.playlist.exists() and self.playlist.read_text().count("#EXTINF") >= 2:
                break
            await asyncio.sleep(0.1)

    async def asyncTearDown(self) -> None:
        if self.generator.poll() is None:
            self.generator.kill()
            self.generator.wait()
        self.tmp.cleanup()

    def make(self) -> tuple[MusicPlayer, FakeResolver, list[str], list[str]]:
        resolver = FakeResolver({"radio": (self.playlist, None)})
        resolver.live.add("radio")  # a search result: it shows to be live only when opened
        notices: list[str] = []
        started: list[str] = []
        player = MusicPlayer(
            resolver,  # type: ignore[arg-type]
            state_file=self.root / "music.json",
            volume=80,
            max_seconds=3,  # far shorter than what we play: livestreams have no length limit
            normalize=False,
            on_change=lambda: None,
            on_notice=notices.append,
            on_active=lambda: None,
            on_track=lambda t: started.append(t.info.title),
        )
        return player, resolver, notices, started

    async def listen(self, player: MusicPlayer, seconds: float) -> np.ndarray:
        """Pull frames in real time, like the mixer."""
        frames = []
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            chunk = player.pull_frame() if player.active else None
            if chunk is not None:
                frames.append(chunk.copy())
            await asyncio.sleep(0.02)
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)

    async def test_plays_past_length_limit_rejoins_after_drop_and_on_resume(self):
        player, resolver, notices, started = self.make()
        player.start()
        await player.add("radio", requested_by="Zasa", source="mumble")
        pcm = await self.listen(player, 7)
        self.assertTrue(player.current.info.is_live, "detected when the stream was opened")
        self.assertGreater(pcm.size / SAMPLE_RATE, 4.5, "plays on beyond MAX_TRACK_MINUTES")
        self.assertAlmostEqual(dominant(pcm), 500, delta=10)

        calls = resolver.stream_calls
        player._decoder.kill()  # dropped connection or expired URL
        pcm = await self.listen(player, 4)
        self.assertGreater(pcm.size / SAMPLE_RATE, 2.0, "back on air after a drop")
        self.assertGreater(resolver.stream_calls, calls, "fresh stream URL after a drop")

        self.assertTrue(player.pause())
        for _ in range(50):
            if player._decoder is None:
                break
            await asyncio.sleep(0.1)
        self.assertIsNone(player._decoder, "a paused livestream does not keep downloading")
        await asyncio.sleep(2)
        self.assertTrue(player.resume())
        pcm = await self.listen(player, 4)
        self.assertGreater(pcm.size / SAMPLE_RATE, 2.0, "plays again after resume")
        self.assertEqual(started, ["Titel radio"], "reconnects do not announce the title again")
        self.assertEqual(notices, [])

        self.generator.terminate()  # broadcaster ends: ffmpeg writes #EXT-X-ENDLIST
        self.generator.wait()
        for _ in range(150):
            if player.current is None:
                break
            await self.listen(player, 0.2)
        await player.close()
        self.assertIsNone(player.current, "an ended livestream ends the track")
        self.assertEqual(notices, ["Der Livestream \"Titel radio\" ist zu Ende."])

    async def test_restart_of_bot_rejoins_live(self):
        player, *_ = self.make()
        player.start()
        await player.add("radio", requested_by="Zasa", source="mumble")
        await self.listen(player, 3)
        await player.close()
        restored, *_ = self.make()
        self.assertEqual(restored.queue[0].start_at, 0.0, "no seeking into a livestream after a restart")
        self.assertTrue(restored.queue[0].info.is_live)


class TimeTest(unittest.TestCase):
    def test_parse_and_format(self):
        self.assertEqual(parse_time("83"), 83)
        self.assertEqual(parse_time("1:23"), 83)
        self.assertEqual(parse_time("1:02:03"), 3723)
        self.assertEqual(parse_time("0:01,5"), 1.5)
        with self.assertRaises(ValueError):
            parse_time("eins")
        self.assertEqual(format_time(83), "1:23")
        self.assertEqual(format_time(3723), "1:02:03")
        self.assertEqual(format_time(None), "?")


if __name__ == "__main__":
    unittest.main()
