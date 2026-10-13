"""The running version is visible without the web interface: /help names commit and date."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import version
from app.platforms.base import IncomingText
from tests.test_commands import make_board

SHA = "703a78c5d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6"


class DescribeTest(unittest.TestCase):
    def test_commit_and_date_from_the_build(self):
        with mock.patch.dict(os.environ, {"BARD_BOARD_REVISION": SHA, "BARD_BOARD_DATE": "2026-10-13"}):
            self.assertEqual(version.describe(), "Version 703a78c vom 13.10.2026")

    def test_local_build_with_uncommitted_changes(self):
        with mock.patch.dict(os.environ, {"BARD_BOARD_REVISION": SHA + "+", "BARD_BOARD_DATE": "2026-10-13"}):
            self.assertEqual(version.describe(), "Version 703a78c+ vom 13.10.2026")

    def test_without_build_info(self):
        with mock.patch.dict(os.environ, {"BARD_BOARD_REVISION": "", "BARD_BOARD_DATE": ""}):
            self.assertEqual(version.describe(), "Version unbekannt")


class HelpShowsVersionTest(unittest.TestCase):
    def test_help_names_the_running_version(self):
        env = {"BARD_BOARD_REVISION": SHA, "BARD_BOARD_DATE": "2026-10-13"}
        for length in (5000, 2000):  # full table, and the short overview for servers with a low limit
            with self.subTest(message_length=length), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, env):
                board = make_board(Path(tmp), message_length=length)
                replies: list[str] = []
                asyncio.run(board.commands.handle(
                    IncomingText(sender="Zasa", sender_id="1", text="!help", reply=replies.append)))
                self.assertEqual(len(replies), 1)
                self.assertLessEqual(len(replies[0]), length)
                self.assertIn("703a78c vom 13.10.2026", replies[0])

    def test_state_for_the_web_interface(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"BARD_BOARD_REVISION": SHA, "BARD_BOARD_DATE": "2026-10-13"}):
            board = make_board(Path(tmp))
            self.assertEqual(board.state_json()["version"], "Version 703a78c vom 13.10.2026")


if __name__ == "__main__":
    unittest.main()
