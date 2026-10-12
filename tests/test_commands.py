"""Chat command replies: one message each, within the server's message length (spam protection)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.config import Config
from app.platforms.base import IncomingText


def make_board(tmp: Path, sounds: int = 3, message_length: int = 5000):
    from app.core import Soundboard

    (tmp / "sounds" / "Kategorie").mkdir(parents=True)
    for i in range(sounds):
        (tmp / "sounds" / "Kategorie" / f"ein ziemlich langer soundname nummer {i:03d}.mp3").write_bytes(b"")
    coll = tmp / "collections"
    for name in ("Archiv 1990", "Podcast 2", "Podcast Extras", "Quiz", "Rufe2"):
        (coll / name / "Gruppe").mkdir(parents=True)
        (coll / name / "Gruppe" / "zeile.mp3").write_bytes(b"")
    cfg = Config(platform="mumble", mumble_host="localhost", data_dir=tmp / "data", sounds_dir=tmp / "sounds",
                 collections_dirs=(coll,), tts_model=tmp / "none.onnx", command_prefixes=["/", "!"])
    board = Soundboard(cfg)
    board.library.scan()
    board.platform.client.message_length = message_length
    return board


class OneMessageTest(unittest.TestCase):
    def run_command(self, board, text: str) -> list[str]:
        replies: list[str] = []

        async def go():
            await board.commands.handle(IncomingText(sender="Zasa", sender_id="1", text=text, reply=replies.append))

        asyncio.run(go())
        return replies

    def test_help_is_one_message_with_all_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = make_board(Path(tmp))
            replies = self.run_command(board, "!help")
            self.assertEqual(len(replies), 1)
            text = replies[0]
            self.assertLessEqual(len(text), 5000)
            for part in ("Sounds", "Musik", "Extras", "Sammlungen", "!sb", "!play", "!tts", "!rufe", "!quiz",
                         "statt ! geht auch /"):
                self.assertIn(part, text)

    def test_help_shrinks_for_small_servers_and_filters_areas(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = make_board(Path(tmp), message_length=2000)
            replies = self.run_command(board, "!help")
            self.assertEqual(len(replies), 1)
            self.assertLessEqual(len(replies[0]), 2000)
            self.assertIn("!help musik", replies[0], "an overview that points to the details")
            musik = self.run_command(board, "!help musik")[0]
            self.assertIn("!skip", musik)
            self.assertNotIn("!tts", musik)

    def test_long_list_is_cut_in_one_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = make_board(Path(tmp), sounds=300, message_length=3000)
            replies = self.run_command(board, "!list")
            self.assertEqual(len(replies), 1)
            self.assertLessEqual(len(replies[0]), 3000)
            self.assertRegex(replies[0], r"… und \d+ weitere - alle im Webinterface")


class CollectionRefreshTest(unittest.TestCase):
    def test_new_collection_gets_a_command_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = make_board(Path(tmp))
            self.assertNotIn("rufe3", board.commands._by_name)
            new = Path(tmp) / "collections" / "Rufe3" / "Gruppe"
            new.mkdir(parents=True)
            (new / "zeile.mp3").write_bytes(b"")
            asyncio.run(board._discover_collections())
            self.assertIn("rufe3", board.commands._by_name)
            self.assertIn("rufe", board.commands._by_name, "existing commands stay")
            help_text = OneMessageTest.run_command(self, board, "!help sammlungen")[0]
            self.assertIn("Rufe3", help_text)


if __name__ == "__main__":
    unittest.main()
