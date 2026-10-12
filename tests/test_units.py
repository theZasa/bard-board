"""Unit tests (python -m unittest discover -s tests)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from app.audio import normalize, trim_silence
from app.commands import CommandHandler, strip_html
from app.config import Config, ConfigError
from app.library import Library, fold, safe_filename
from app.mixer import Mixer
from app.mumble.protocol import build_opus_packet, decode_varint, encode_varint, parse_server_voice_packet
from app.opus import FRAME_SAMPLES, SAMPLE_RATE, Decoder, Encoder


class VarintTest(unittest.TestCase):
    def test_roundtrip(self):
        for value in [0, 1, 0x7F, 0x80, 0x3FFF, 0x4000, 0x1FFFFF, 0x200000, 0xFFFFFFF, 0x10000000, 2**32 - 1, 2**40]:
            encoded = encode_varint(value)
            self.assertEqual(decode_varint(encoded), (value, len(encoded)), value)

    def test_voice_packet_roundtrip(self):
        packet = build_opus_packet(b"\x01\x02\x03", sequence=42, terminator=True)
        # Server relayed packets carry the sender session after the header byte.
        relayed = packet[:1] + encode_varint(7) + packet[1:]
        parsed = parse_server_voice_packet(relayed)
        self.assertEqual((parsed.session, parsed.sequence, parsed.opus, parsed.terminator), (7, 42, b"\x01\x02\x03", True))


class MurmurLeakyBucket:
    """Reference: Murmur 1.3 LeakyBucket::ratelimit (integer drain, timer restarted on every call)."""

    def __init__(self, per_sec: int = 1, burst: int = 5) -> None:
        self.per_sec, self.burst, self.tokens, self.last_ms = per_sec, burst, 0, None

    def limited(self, now_ms: int) -> bool:
        elapsed = 0 if self.last_ms is None else now_ms - self.last_ms
        self.last_ms = now_ms
        drain = elapsed * self.per_sec // 1000
        self.tokens = max(0, self.tokens - drain)
        if self.tokens + 1 <= self.burst:
            self.tokens += 1
            return False
        return True


class RateLimitTest(unittest.TestCase):
    def test_paced_messages_are_never_dropped_by_murmur(self):
        import random

        from app.mumble.protocol import ServerRateLimit

        rng = random.Random(42)
        for _ in range(200):
            model, server = ServerRateLimit(), MurmurLeakyBucket()
            now, arrival, dropped, sent = 0.0, 0, 0, 0
            for _ in range(rng.randint(5, 40)):
                now += rng.choice([0.0, 0.05, 0.3, 0.7, 1.0, 2.5])  # bursts and pauses
                wait = model.wait_time(now)
                now += wait
                model.consume(now)
                # network delay towards the server varies, but TCP delivers in order
                arrival = max(arrival, int((now + rng.uniform(0, 0.1)) * 1000))
                dropped += server.limited(arrival)
                sent += 1
            self.assertEqual(dropped, 0, f"{dropped} of {sent} messages dropped")

    def test_naive_token_bucket_would_be_dropped(self):
        server = MurmurLeakyBucket()
        times = [0, 100, 200, 300, 400] + [400 + 900 * i for i in range(1, 6)]  # 0.9 s spacing
        self.assertTrue(any(server.limited(t) for t in times), "reference bucket must drop sub-second traffic")


class OpusTest(unittest.TestCase):
    def test_encode_decode_keeps_frequency(self):
        enc, dec = Encoder(64000), Decoder()
        t = np.arange(FRAME_SAMPLES * 25) / SAMPLE_RATE
        pcm = (np.sin(2 * np.pi * 1000 * t) * 12000).astype(np.int16)
        out = b"".join(dec.decode(enc.encode(pcm[i : i + FRAME_SAMPLES].tobytes())) for i in range(0, pcm.size, FRAME_SAMPLES))
        decoded = np.frombuffer(out, dtype=np.int16)[FRAME_SAMPLES * 5 :].astype(np.float32)
        freqs = np.fft.rfftfreq(decoded.size, 1 / SAMPLE_RATE)
        self.assertAlmostEqual(freqs[np.argmax(np.abs(np.fft.rfft(decoded)))], 1000, delta=10)


    def test_no_pointer_truncation_with_high_heap(self):
        # Regression: without argtypes ctypes truncated the encoder pointer to 32 bit, which crashed
        # (segfault) in the Docker image where the heap lives above 4 GiB. Force high addresses.
        import subprocess
        import sys

        code = (
            "from app.opus import Encoder, Decoder; e = Encoder(64000); d = Decoder(); "
            "assert e._st > 2**32, hex(e._st); e.set_bitrate(32000); "
            "print(len(d.decode(e.encode(bytes(1920)))))"
        )
        env = dict(os.environ, MALLOC_MMAP_THRESHOLD_="0")
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1920")


class RetryableTest(unittest.TestCase):
    def test_classification(self):
        from app.media import is_retryable

        self.assertTrue(is_retryable("Error opening input files: Server returned 403 Forbidden (access denied)"))
        self.assertTrue(is_retryable("HTTP Error 429: Too Many Requests"))
        self.assertTrue(is_retryable("Connection refused"))
        self.assertFalse(is_retryable("Error opening input files: Invalid argument"))
        self.assertFalse(is_retryable("Invalid data found when processing input"))


class AudioTest(unittest.TestCase):
    def test_normalize_quiet_and_loud_to_similar_level(self):
        t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
        quiet = (np.sin(2 * np.pi * 440 * t) * 2000).astype(np.int16)
        loud = (np.sin(2 * np.pi * 440 * t) * 30000).astype(np.int16)
        rms = lambda x: float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))  # noqa: E731
        self.assertLess(abs(20 * np.log10(rms(normalize(quiet)) / rms(normalize(loud)))), 1.0)
        self.assertLessEqual(int(np.max(np.abs(normalize(loud).astype(np.int32)))), 32767)

    def test_normalize_respects_peak_limit(self):
        spiky = np.zeros(SAMPLE_RATE, dtype=np.int16)
        spiky[::4800] = 30000  # very low RMS, very high peak
        self.assertLessEqual(int(np.max(normalize(spiky))), int(32767 * 10 ** (-1 / 20)) + 2)

    def test_trim_silence(self):
        pcm = np.concatenate([np.zeros(24000), np.full(4800, 8000), np.zeros(24000)]).astype(np.int16)
        trimmed = trim_silence(pcm)
        self.assertLess(trimmed.size, 4800 + 2 * SAMPLE_RATE // 100 + 1)
        self.assertGreaterEqual(trimmed.size, 4800)


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.sounds = root / "sounds"
        for rel in ["alter käse.mp3", "alter freund.mp3", "Memes/tada.ogg", "Memes/tada fanfare.mp3", "aussortiert/alt.mp3", "notes.txt"]:
            p = self.sounds / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        self.lib = Library(self.sounds, root / "library.json", root / "trash", ["aussortiert"])
        self.lib.scan()

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, match):
        return [s.name for s in match.sounds]

    def test_scan_ignores_folders_and_non_audio(self):
        self.assertEqual(sorted(s.rel for s in self.lib.sounds.values()),
                         ["Memes/tada fanfare.mp3", "Memes/tada.ogg", "alter freund.mp3", "alter käse.mp3"])
        self.assertEqual(self.lib.categories, ["", "Memes"])

    def test_resolve(self):
        self.assertEqual(self.names(self.lib.resolve("alter kase")), ["alter käse"])
        self.assertEqual(self.names(self.lib.resolve("ALTER KÄSE")), ["alter käse"])
        self.assertTrue(self.lib.resolve("tada").exact)
        self.assertEqual(self.names(self.lib.resolve("memes/tada")), ["tada"])
        self.assertEqual(sorted(self.names(self.lib.resolve("alter"))), ["alter freund", "alter käse"])
        self.assertEqual(self.names(self.lib.resolve("fanfare")), ["tada fanfare"])
        self.assertEqual(self.names(self.lib.resolve("alter frend")), ["alter freund"])  # typo
        self.assertEqual(self.lib.resolve("zzzz").sounds, [])

    def test_metadata_survives_rename_and_rescan(self):
        sound = self.lib.resolve("alter freund").unique
        sound.favorite, sound.plays = True, 3
        sound.speaker, sound.text = "Paula Berger", "Alter Freund, was?"  # lines taken from a collection
        renamed = self.lib.rename(sound, "bester freund", "Freunde")
        self.assertEqual((renamed.category, renamed.favorite, renamed.plays), ("Freunde", True, 3))
        self.assertEqual((renamed.speaker, renamed.text), ("Paula Berger", "Alter Freund, was?"))
        lib2 = Library(self.sounds, self.lib.meta_file, self.lib.trash_dir, ["aussortiert"])
        lib2.scan()
        again = lib2.resolve("bester freund").unique
        self.assertEqual((again.favorite, again.plays, again.speaker), (True, 3, "Paula Berger"))
        self.assertEqual([s.name for s in lib2.search("paula")], ["bester freund"], "the speaker is searchable")

    def test_delete_moves_to_trash(self):
        target = self.lib.delete(self.lib.resolve("alter freund").unique)
        self.assertTrue(target.exists())
        self.assertIsNone(self.lib.resolve("alter freund").unique)

    def test_safe_filename(self):
        self.assertEqual(safe_filename('a<b>:c"d|e?f*.mp3'), "abcdef.mp3")
        with self.assertRaises(ValueError):
            safe_filename("CON.mp3")
        self.assertEqual(fold("Größe Ärger!"), "grosse arger")


class FakeSink:
    """Records what the mixer hands to the platform: False per audio frame, True for an end."""

    ready = True

    def __init__(self):
        self.frames = []

    def send_frame(self, pcm):
        assert len(pcm) == FRAME_SAMPLES * 2
        self.frames.append(False)

    def end(self):
        self.frames.append(True)

    async def drain(self):
        pass


class MixerTest(unittest.TestCase):
    def test_mix_and_limit(self):
        mixer = Mixer(FakeSink(), max_voices=2, overlap=True, volume=100)
        loud = np.full(FRAME_SAMPLES * 2, 30000, dtype=np.int16)
        mixer.play("a", "a", loud, gain=1, requested_by="t", source="t")
        mixer.play("b", "b", loud, gain=1, requested_by="t", source="t")
        mixer.play("c", "c", loud, gain=1, requested_by="t", source="t")
        self.assertEqual([v.sound_id for v in mixer.voices], ["b", "c"])  # oldest dropped
        frame = mixer.mix_frame()
        self.assertTrue(np.all(frame <= 32767) and np.all(frame > 30000))  # soft limited, not wrapped
        mixer.mix_frame()
        self.assertEqual(mixer.voices, [])

    def test_stream_ends_with_terminator_also_when_stopped(self):
        async def run():
            sink = FakeSink()
            mixer = Mixer(sink, max_voices=4, overlap=True, volume=100)
            mixer.start()
            mixer.play("a", "a", np.ones(SAMPLE_RATE, dtype=np.int16), gain=1, requested_by="t", source="t")
            await asyncio.sleep(0.15)
            mixer.stop()
            await asyncio.sleep(0.05)
            await mixer.close()
            return sink.frames

        frames = asyncio.run(run())
        self.assertTrue(frames[-1])
        self.assertEqual(frames.count(True), 1)
        self.assertLess(len(frames), 20)


class CommandParsingTest(unittest.TestCase):
    def test_strip_html(self):
        self.assertEqual(strip_html("<p>/sb&nbsp;alter&amp;k&auml;se</p>"), "/sb alter&käse")
        self.assertEqual(strip_html('/sb <a href="x">tada</a><br/>'), "/sb tada")

    def test_parse_prefixes(self):
        handler = CommandHandler.__new__(CommandHandler)
        handler.cfg = SimpleNamespace(command_prefixes=["/", "!"])
        self.assertEqual(handler._parse("/SB  Alter Käse"), ("/", "sb", "Alter Käse"))
        self.assertEqual(handler._parse("!stop"), ("!", "stop", ""))
        self.assertIsNone(handler._parse("hallo /sb"))
        self.assertIsNone(handler._parse("/"))


class ConfigTest(unittest.TestCase):
    def test_requires_host(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError):
                Config.from_env()

    def test_parses_values(self):
        env = {"MUMBLE_HOST": "example.org", "ADMINS": "Zasa, Noah", "OVERLAP": "nein", "COMMAND_COOLDOWN": "0,5"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.admins, ["Zasa", "Noah"])
        self.assertFalse(cfg.overlap)
        self.assertEqual(cfg.command_cooldown, 0.5)
        self.assertTrue(cfg.is_admin("zasa"))
        self.assertFalse(cfg.is_admin("Pia"))

    def test_rejects_invalid(self):
        with mock.patch.dict(os.environ, {"MUMBLE_HOST": "x", "MUMBLE_PORT": "abc"}, clear=True):
            with self.assertRaises(ConfigError):
                Config.from_env()


if __name__ == "__main__":
    unittest.main()
