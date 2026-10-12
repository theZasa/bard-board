"""yt-dlp stays current without a new image: daily check, restart only while nothing plays."""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import core, updater
from app.config import Config


def make_board(tmp: Path):
    (tmp / "sounds").mkdir()
    cfg = Config(platform="mumble", mumble_host="localhost", data_dir=tmp / "data", sounds_dir=tmp / "sounds",
                 collections_dirs=(), tts_model=tmp / "none.onnx", command_prefixes=["/"])
    return core.Soundboard(cfg)


class VersionTest(unittest.TestCase):
    def test_pypi_and_ytdlp_spellings_compare_as_dates(self):
        self.assertTrue(updater.is_newer("2026.10.2", "2026.08.19"))
        self.assertTrue(updater.is_newer("2026.8.19.1", "2026.08.19"))
        self.assertFalse(updater.is_newer("2026.8.19", "2026.08.19"))
        self.assertFalse(updater.is_newer("2026.7.1", "2026.08.19"))

    def test_unknown_versions_never_trigger_a_restart(self):
        self.assertFalse(updater.is_newer(None, "2026.08.19"))
        self.assertFalse(updater.is_newer("2026.10.2", None))

    def test_latest_version_reads_pypi_and_survives_errors(self):
        body = io.BytesIO(json.dumps({"info": {"version": "2026.10.2"}}).encode())
        with mock.patch("urllib.request.urlopen", return_value=body):
            self.assertEqual(updater.latest_version(), "2026.10.2")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(updater.latest_version())


class RestartTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.board = make_board(Path(self._tmp.name))
        self.hook = mock.Mock()
        self.board.shutdown_hook = self.hook
        patches = [
            mock.patch.object(updater, "CHECK_INTERVAL", 0),
            mock.patch.object(core, "IDLE_POLL", 0.01),
            mock.patch.object(updater, "installed_version", return_value="2026.08.19"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    async def test_newer_release_restarts_once_nothing_plays(self):
        self.board.pull = {"seconds": 10}  # a running pull timer must not be cut off
        with mock.patch.object(updater, "latest_version", return_value="2026.10.2"):
            loop = asyncio.create_task(self.board._ytdlp_loop())
            await asyncio.sleep(0.1)
            self.hook.assert_not_called()
            self.assertFalse(self.board.restart_requested)
            self.board.pull = None
            await asyncio.wait_for(loop, 2)
        self.hook.assert_called_once()
        self.assertTrue(self.board.restart_requested)
        self.assertIn("yt-dlp 2026.10.2", self.board.activity[-1].text)

    async def test_same_release_keeps_running(self):
        with mock.patch.object(updater, "latest_version", return_value="2026.8.19") as latest:
            loop = asyncio.create_task(self.board._ytdlp_loop())
            await asyncio.sleep(0.1)
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
        self.assertGreater(latest.call_count, 1, "checks again later")
        self.hook.assert_not_called()
        self.assertFalse(self.board.restart_requested)

    def test_playing_music_or_sounds_is_not_idle(self):
        self.assertTrue(self.board.is_idle())
        with mock.patch.object(type(self.board.mixer), "playing", new_callable=mock.PropertyMock, return_value=True):
            self.assertFalse(self.board.is_idle())
        with mock.patch.object(type(self.board.music), "state", new_callable=mock.PropertyMock, return_value="playing"):
            self.assertFalse(self.board.is_idle())


if __name__ == "__main__":
    unittest.main()
