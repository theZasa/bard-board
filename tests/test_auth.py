"""Password protection of the web interface (HTTP Basic auth plus cookie for the WebSocket)."""

from __future__ import annotations

import asyncio
import base64
import unittest
from unittest import mock

from app import auth
from app.auth import COOKIE, BasicAuth


async def inner(scope, receive, send):
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def call(app, path="/", headers=None, kind="http", scheme="http"):
    sent = []

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    scope = {"type": kind, "path": path, "scheme": scheme,
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    asyncio.run(app(scope, receive, send))
    return sent


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class BasicAuthTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(auth, "FAILURE_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = BasicAuth(inner, "geh:eim ä")

    def status(self, sent):
        return sent[0].get("status")

    def test_rejects_without_or_with_wrong_password(self):
        sent = call(self.app)
        self.assertEqual(self.status(sent), 401)
        self.assertIn((b"www-authenticate", 'Basic realm="Soundboard", charset="UTF-8"'.encode()), sent[0]["headers"])
        self.assertEqual(self.status(call(self.app, headers={"Authorization": basic("Zasa", "falsch")})), 401)
        self.assertEqual(self.status(call(self.app, headers={"Authorization": "Basic %%%"})), 401)
        self.assertEqual(self.status(call(self.app, headers={"Authorization": "Bearer x"})), 401)

    def test_any_user_name_and_cookie_for_later(self):
        sent = call(self.app, headers={"Authorization": basic("Zasa", "geh:eim ä")}, scheme="https")
        self.assertEqual(self.status(sent), 200)
        cookie = dict(sent[0]["headers"])[b"set-cookie"].decode()
        self.assertTrue(cookie.startswith(f"{COOKIE}="))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        value = cookie.split(";")[0]
        self.assertEqual(self.status(call(self.app, headers={"Cookie": value})), 200)
        ws = call(self.app, headers={"Cookie": value}, kind="websocket")
        self.assertEqual(ws[0]["type"], "websocket.accept")

    def test_websocket_without_login_is_closed(self):
        ws = call(self.app, kind="websocket")
        self.assertEqual(ws[0], {"type": "websocket.close", "code": 4401})
        forged = call(self.app, headers={"Cookie": f"{COOKIE}=abc"}, kind="websocket")
        self.assertEqual(forged[0]["type"], "websocket.close")

    def test_health_check_stays_open(self):
        self.assertEqual(self.status(call(self.app, path="/healthz")), 200)

    def test_cookie_changes_with_password(self):
        sent = call(self.app, headers={"Authorization": basic("x", "geh:eim ä")})
        value = dict(sent[0]["headers"])[b"set-cookie"].decode().split(";")[0]
        other = BasicAuth(inner, "neu")
        self.assertEqual(self.status(call(other, headers={"Cookie": value})), 401)


if __name__ == "__main__":
    unittest.main()
