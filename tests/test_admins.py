"""Admins by Discord user ID (DISCORD_ADMINS): chat admin commands and what the web interface allows."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.auth import SESSION_COOKIE, WebLogin
from tests.test_auth import asgi

try:
    import discord  # noqa: F401
    HAVE_DISCORD = True
except ImportError:
    HAVE_DISCORD = False

ADMIN, OTHER = "111111111111111111", "222222222222222222"


def member(mid: str, *, server_admin: bool = False, roles=()):
    return SimpleNamespace(id=int(mid), name="x", display_name="x", roles=list(roles),
                           guild_permissions=SimpleNamespace(administrator=server_admin))


@unittest.skipUnless(HAVE_DISCORD, "discord.py fehlt")
class ChatAdminTest(unittest.TestCase):
    def platform(self, **cfg):
        from tests.test_discord import make_board

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        return make_board(Path(self._tmp.name), **cfg).platform

    def test_admins_by_discord_user_id(self):
        p = self.platform(discord_admins=[ADMIN])
        self.assertTrue(p.is_admin(member(ADMIN)))
        self.assertFalse(p.is_admin(member(OTHER)), "once DISCORD_ADMINS is set, others are no admins")
        self.assertTrue(p.is_admin(member(OTHER, server_admin=True)), "server admins stay admins")

    def test_without_any_admin_setting_everybody_may(self):
        self.assertTrue(self.platform().is_admin(member(OTHER)))


@unittest.skipUnless(HAVE_DISCORD, "discord.py fehlt")
class WebAdminTest(unittest.TestCase):
    def setUp(self):
        from app.web import create_app
        from tests.test_discord import make_board

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.board = make_board(Path(self._tmp.name), discord_admins=[ADMIN])
        self.board.web_login = WebLogin(Path(self._tmp.name) / "anmeldung.json", "Discord")
        self.app = create_app(self.board)
        self.cookies = {}
        for name, uid in (("Chef", ADMIN), ("Gast", OTHER)):
            self.board.web_login._remember(f"discord:{uid}", name)
            token = self.board.web_login.new_session(f"discord:{uid}", passkey=False)
            self.cookies[name] = f"{SESSION_COOKIE}={token}"
        self.sound = next(iter(self.board.library.sounds.values()))

    def call(self, who: str, method: str, path: str, body: dict | None = None):
        status, data, _ = asgi(self.app, method, path, body, cookie=self.cookies[who])
        return status, data

    def test_status_tells_the_interface_who_is_admin(self):
        self.assertTrue(self.call("Chef", "GET", "/auth/status")[1]["admin"])
        self.assertFalse(self.call("Gast", "GET", "/auth/status")[1]["admin"])

    def test_managing_sounds_is_for_admins(self):
        sid = self.sound.id
        for method, path, body in (("PATCH", f"/api/sounds/{sid}", {"name": "neu"}),
                                   ("PATCH", f"/api/sounds/{sid}", {"volume": 50}),
                                   ("PATCH", f"/api/sounds/{sid}", {"category": "X"}),
                                   ("DELETE", f"/api/sounds/{sid}", None),
                                   ("POST", "/api/upload", None),
                                   ("POST", "/api/clip", {"url": "https://x.y/z", "start": "0", "end": "1"}),
                                   ("PUT", "/api/youtube-cookies", None),
                                   ("DELETE", "/api/youtube-cookies", None),
                                   ("PUT", "/api/radio", {"name": "lofi", "url": "https://x.y/z"}),
                                   ("DELETE", "/api/radio/lofi", None)):
            with self.subTest(method=method, path=path):
                status, data = self.call("Gast", method, path, body)
                self.assertEqual(status, 403)
                self.assertIn("nur Admins", data["error"])
        self.assertEqual(self.call("Gast", "PATCH", f"/api/sounds/{sid}", {"favorite": True})[0], 200,
                         "favorites are for everyone")
        self.assertEqual(self.call("Chef", "PATCH", f"/api/sounds/{sid}", {"volume": 50})[0], 200)
        self.assertEqual(self.call("Chef", "DELETE", f"/api/sounds/{sid}")[0], 200)

    def test_own_join_sound_and_aliases_for_everyone_others_only_for_admins(self):
        sid = self.sound.id
        self.assertEqual(self.call("Gast", "PUT", "/api/joinsounds", {"user": "Gast", "sound_id": sid})[0], 200)
        self.assertEqual(self.call("Gast", "PUT", "/api/joinsounds", {"user": "Chef", "sound_id": sid})[0], 403)
        self.assertEqual(self.call("Gast", "DELETE", "/api/joinsounds/Gast")[0], 200)
        self.assertEqual(self.call("Chef", "PUT", "/api/joinsounds", {"user": "Gast", "sound_id": sid})[0], 200)
        self.assertEqual(self.call("Chef", "PUT", "/api/aliases", {"name": "td", "target": self.sound.name})[0], 200)
        self.assertEqual(self.call("Gast", "PUT", "/api/aliases", {"name": "td", "target": self.sound.name})[0], 403)
        self.assertEqual(self.call("Gast", "DELETE", "/api/aliases/td")[0], 403)
        self.assertEqual(self.call("Gast", "PUT", "/api/aliases", {"name": "meins", "target": self.sound.name})[0], 200)
        self.assertEqual(self.call("Gast", "DELETE", "/api/aliases/meins")[0], 200)

    def test_without_discord_admins_everybody_may(self):
        from dataclasses import replace

        self.board.cfg = replace(self.board.cfg, discord_admins=[])
        self.assertTrue(self.call("Gast", "GET", "/auth/status")[1]["admin"])
        self.assertEqual(self.call("Gast", "DELETE", f"/api/sounds/{self.sound.id}")[0], 200)


class OpenInterfacesTest(unittest.TestCase):
    def test_local_and_mumble_interfaces_stay_open(self):
        from app.web import create_app
        from tests.test_commands import make_board

        with tempfile.TemporaryDirectory() as tmp:
            board = make_board(Path(tmp))  # Mumble, no login
            app = create_app(board)
            sound = next(iter(board.library.sounds.values()))
            self.assertEqual(asgi(app, "PATCH", f"/api/sounds/{sound.id}", {"volume": 40})[0], 200)


if __name__ == "__main__":
    unittest.main()
