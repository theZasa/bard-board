"""Messages a server integration relays into the Mumble chat ("noah(via Steam): /tada").

They come from the server itself (no sender, the client shows "Server"). The bot treats them as
written by the named person, but cannot verify that name: no admin commands, no /login.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.mumble.client import TextMessage, User
from app.platforms.base import IncomingText
from app.platforms.mumble import parse_relayed
from tests.test_commands import make_board

BOT, ME_CHANNEL = 5, 3


class ParseTest(unittest.TestCase):
    def test_relay_format(self):
        self.assertEqual(parse_relayed("noah(via Steam): uiae"), ("noah", "Steam", "uiae"))
        self.assertEqual(parse_relayed("Ole Marie (via Discord):  /sb tada "), ("Ole Marie", "Discord", "/sb tada"))
        self.assertEqual(parse_relayed("noah(via Steam): erste\nzweite"), ("noah", "Steam", "erste\nzweite"))

    def test_other_server_messages_are_ignored(self):
        for text in ("Willkommen auf dem Server", "noah: /tada", "(via Steam): /tada", "noah(via Steam):", ""):
            with self.subTest(text):
                self.assertIsNone(parse_relayed(text))


class AdapterTest(unittest.TestCase):
    """The Mumble adapter with the message exactly as the server delivers it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.board = make_board(Path(self._tmp.name))
        client = self.board.platform.client
        client.session = BOT
        client.users = {BOT: User(session=BOT, name="Bard", channel_id=ME_CHANNEL),
                        9: User(session=9, name="Zasa", channel_id=ME_CHANNEL, user_id=4)}
        self.sent: list[tuple[str, dict]] = []
        client.send_text = lambda html, **where: self.sent.append((html, where))
        self.seen: list[IncomingText] = []

        async def on_text(msg):
            self.seen.append(msg)
        self.board.on_text = on_text

    def tearDown(self):
        self._tmp.cleanup()

    def deliver(self, msg: TextMessage) -> None:
        asyncio.run(self.board.platform.on_text_message(msg))

    def test_relayed_message_counts_as_written_by_the_named_person(self):
        self.deliver(TextMessage(actor=None, sender="", message="noah(via Steam): /tada", channel_ids=[ME_CHANNEL]))
        (msg,) = self.seen
        self.assertEqual((msg.sender, msg.text, msg.relayed, msg.is_admin, msg.private), ("noah", "/tada", True, False, False))
        self.assertEqual(msg.sender_id, "relay:steam:noah")
        msg.reply("ok")
        self.assertEqual(self.sent, [("ok", {"channel_id": ME_CHANNEL})], "answers go to the channel")

    def test_users_cannot_pretend_to_be_relayed(self):
        self.deliver(TextMessage(actor=9, sender="Zasa", message="boss(via Steam): /vol 100", channel_ids=[ME_CHANNEL]))
        (msg,) = self.seen
        self.assertEqual((msg.sender, msg.relayed), ("Zasa", False))

    def test_other_server_messages_and_other_channels_are_ignored(self):
        self.deliver(TextMessage(actor=None, sender="", message="Wartung um 22 Uhr", channel_ids=[ME_CHANNEL]))
        self.deliver(TextMessage(actor=None, sender="", message="noah(via Steam): /tada", channel_ids=[99]))
        self.assertEqual(self.seen, [])


class CommandTest(unittest.TestCase):
    def run_cmd(self, text: str) -> list[str]:
        replies: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            board = make_board(Path(tmp))
            msg = IncomingText(sender="noah", sender_id="relay:steam:noah", text=text, reply=replies.append,
                               is_admin=False, relayed=True)
            asyncio.run(board.commands.handle(msg))
        return replies

    def test_no_admin_commands_via_relay_even_without_admin_list(self):
        self.assertIn("nicht über eine Weiterleitung", self.run_cmd("/vol 100")[0])

    def test_no_login_via_relay(self):
        self.assertIn("direkt", self.run_cmd("/login ABCD-EFGH")[0])

    def test_normal_commands_work(self):
        self.assertIn("Soundboard-Befehle", self.run_cmd("/help")[0])


if __name__ == "__main__":
    unittest.main()
