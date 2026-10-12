"""Tests for join sound and alias storage and the synthetic pull timer sounds."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.extras import Aliases, JoinSounds
from app.opus import SAMPLE_RATE
from app.tts import Speech, beep, clean_text, war_horn


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_join_sounds_case_insensitive_and_persistent(self):
        js = JoinSounds(self.root / "j.json")
        js.set("Zasa", "beep.mp3", "Zasa")
        self.assertEqual(JoinSounds(self.root / "j.json").get("zasa"), "beep.mp3")
        js.sound_moved("beep.mp3", "Memes/beep.mp3")
        self.assertEqual(JoinSounds(self.root / "j.json").get("ZASA"), "Memes/beep.mp3")
        js.sound_moved("Memes/beep.mp3", None)
        self.assertIsNone(JoinSounds(self.root / "j.json").get("Zasa"))

    def test_aliases_validation(self):
        al = Aliases(self.root / "a.json")
        self.assertEqual(al.validate_name("LR", {"play"}), "lr")
        for bad in ("play", "zwei worte", "x" * 30, "a/b"):
            with self.assertRaises(ValueError):
                al.validate_name(bad, {"play"})
        al.set("LR", "leeroy", "Zasa")
        self.assertEqual(Aliases(self.root / "a.json").get("lr")["target"], "leeroy")
        self.assertTrue(al.remove("Lr"))
        self.assertFalse(al.remove("lr"))

    def test_broken_file_starts_empty(self):
        (self.root / "b.json").write_text("{kaputt")
        self.assertEqual(JoinSounds(self.root / "b.json").entries, {})


class SignalTest(unittest.TestCase):
    def test_war_horn_is_low_and_loud(self):
        horn = war_horn()
        self.assertAlmostEqual(horn.size / SAMPLE_RATE, 1.8, delta=0.01)
        spectrum = np.abs(np.fft.rfft(horn.astype(np.float32)))
        freqs = np.fft.rfftfreq(horn.size, 1 / SAMPLE_RATE)
        self.assertLess(freqs[np.argmax(spectrum)], 400)
        self.assertGreater(float(np.sqrt(np.mean(horn.astype(np.float64) ** 2))), 2000)

    def test_beep_and_text_cleanup(self):
        self.assertEqual(beep().size, int(SAMPLE_RATE * 0.15))
        self.assertEqual(clean_text("  Hallo \n  Welt ", 100), "Hallo Welt")
        self.assertEqual(clean_text("eins zwei drei vier", 12), "eins zwei ...")


class SpeechAvailabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.model = Path(self.tmp.name) / "piper" / "voice.onnx"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_model(self):
        speech = Speech(self.model, max_chars=100)
        self.assertFalse(speech.available)
        self.assertIn("Kein Stimmenmodell", speech.unavailable_reason)

    def test_unreadable_model_never_raises(self):
        # Regression: an unreadable /opt/piper (wrong folder mode in the image) raised
        # PermissionError from the status endpoint and took the web interface down.
        from unittest import mock

        self.model.parent.mkdir()
        self.model.write_bytes(b"x")
        Path(f"{self.model}.json").write_text("{}")
        denied = PermissionError(13, "Permission denied", str(self.model))
        with mock.patch.object(Path, "open", side_effect=denied):
            speech = Speech(self.model, max_chars=100)
            self.assertFalse(speech.available)
            self.assertIn("nicht lesbar", speech.unavailable_reason)
            self.assertIn("Permission denied", speech.unavailable_reason)


if __name__ == "__main__":
    unittest.main()
