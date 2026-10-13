"""Web login: code via the chat (device flow), passkeys, sessions and the gate in front of the app."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from app.auth import (CODE_TTL, MAX_FAILURES, PASSKEY_SESSION_DAYS, SESSION_COOKIE, LoginError, LoginGate, WebLogin,
                      device_label, next_midnight)
from app.platforms.base import IncomingText
from app.platforms.mumble import mumble_account

ORIGIN = "https://soundboard.example.org"
RP_ID = "soundboard.example.org"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SoftAuthenticator:
    """A passkey without a fingerprint: ES256 key, "none" attestation, like a platform authenticator."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.counter = 0

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()

    def create(self, options: dict, origin: str = ORIGIN) -> dict:
        client_data = self._client_data("webauthn.create", options["challenge"], origin)
        auth_data = (hashlib.sha256(options["rp"]["id"].encode()).digest() + bytes([0x45]) + struct.pack(">I", 0)
                     + bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + self._cose())
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64u(self.credential_id), "rawId": b64u(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client_data), "attestationObject": b64u(attestation)},
                "clientExtensionResults": {}}

    def get(self, options: dict, origin: str = ORIGIN) -> dict:
        self.counter += 1
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        auth_data = hashlib.sha256(options["rpId"].encode()).digest() + bytes([0x05]) + struct.pack(">I", self.counter)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.credential_id), "rawId": b64u(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client_data), "authenticatorData": b64u(auth_data),
                             "signature": b64u(signature), "userHandle": None},
                "clientExtensionResults": {}}


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class LoginTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "anmeldung.json"
        self.clock = Clock()
        self.login = WebLogin(self.path, "Mumble", clock=self.clock)

    def tearDown(self):
        self._tmp.cleanup()

    def code_session(self, account="mumble:7", name="Zasa") -> str:
        flow, code = self.login.start_code()
        self.login.confirm_code(code, account, name)
        state, token = self.login.poll_code(flow)
        self.assertEqual(state, "ok")
        return token


class CodeFlowTest(LoginTestCase):
    def test_code_from_the_chat_unlocks_exactly_that_browser(self):
        flow, code = self.login.start_code()
        self.assertRegex(code, r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertEqual(self.login.poll_code(flow), ("pending", None))
        self.login.confirm_code(code.lower().replace("-", " "), "mumble:7", "Zasa")  # typed sloppily
        state, token = self.login.poll_code(flow)
        self.assertEqual(state, "ok")
        session = self.login.session(token)
        self.assertEqual(self.login.name_of(session["account"]), "Zasa")
        self.assertFalse(session["passkey"])
        self.assertEqual(session["expires"], next_midnight(self.clock.now), "without a passkey: until midnight")
        self.assertEqual(self.login.poll_code(flow), ("expired", None), "a code is used up")

    def test_used_or_expired_codes_are_rejected(self):
        flow, code = self.login.start_code()
        self.clock.now += CODE_TTL + 1
        with self.assertRaises(LoginError):
            self.login.confirm_code(code, "mumble:7", "Zasa")
        self.assertEqual(self.login.poll_code(flow), ("expired", None))
        flow, code = self.login.start_code()
        self.login.confirm_code(code, "mumble:7", "Zasa")
        with self.assertRaises(LoginError, msg="someone else reading the code along cannot take it over"):
            self.login.confirm_code(code, "mumble:9", "Eve")

    def test_guessing_codes_is_limited(self):
        for _ in range(MAX_FAILURES):
            with self.assertRaises(LoginError):
                self.login.confirm_code("AAAA-AAAA", "mumble-name:eve", "Eve")
        _, code = self.login.start_code()
        with self.assertRaisesRegex(LoginError, "Zu viele"):
            self.login.confirm_code(code, "mumble-name:eve", "Eve")
        self.login.confirm_code(code, "mumble:7", "Zasa")  # others are not affected

    def test_sessions_survive_a_restart_and_end_on_logout(self):
        token = self.code_session()
        again = WebLogin(self.path, "Mumble", clock=self.clock)
        self.assertIsNotNone(again.session(token))
        again.end_session(token)
        self.assertIsNone(WebLogin(self.path, "Mumble", clock=self.clock).session(token))
        self.assertIsNone(self.login.session("erfunden"))

    def test_session_without_passkey_ends_at_midnight(self):
        token = self.code_session()
        self.clock.now = next_midnight(self.clock.now) + 1
        self.assertIsNone(self.login.session(token))

    def test_a_broken_file_is_kept_aside(self):
        self.path.write_text("{kaputt", "utf-8")
        login = WebLogin(self.path, "Mumble", clock=self.clock)
        self.assertEqual(login.store.passkeys, [])
        self.assertTrue(self.path.with_suffix(".kaputt.json").exists())


class PasskeyTest(LoginTestCase):
    def register(self, token: str, device: SoftAuthenticator) -> None:
        options = json.loads(self.login.registration_options(token, RP_ID))
        self.assertEqual(options["authenticatorSelection"]["residentKey"], "required")
        self.login.register(token, device.create(options), RP_ID, ORIGIN, "Windows · Chrome")

    def passkey_login(self, device: SoftAuthenticator, origin: str = ORIGIN) -> str:
        flow, options = self.login.login_options(RP_ID)
        return self.login.login(flow, device.get(json.loads(options), origin), RP_ID, ORIGIN)

    def test_register_then_sign_in_without_the_chat(self):
        device = SoftAuthenticator()
        token = self.code_session()
        self.register(token, device)
        session = self.login.session(token)
        self.assertTrue(session["passkey"], "the code session is upgraded right away")
        self.assertEqual(session["expires"], self.clock.now + PASSKEY_SESSION_DAYS * 86400)
        self.clock.now += 3 * 86400
        fresh = WebLogin(self.path, "Mumble", clock=self.clock)  # after a restart
        flow, options = fresh.login_options(RP_ID)
        new = fresh.login(flow, device.get(json.loads(options)), RP_ID, ORIGIN)
        self.assertEqual(fresh.name_of(fresh.session(new)["account"]), "Zasa")
        listed = fresh.passkeys_of("mumble:7")
        self.assertEqual([p["label"] for p in listed], ["Windows · Chrome"])
        self.assertEqual(listed[0]["last_used"], self.clock.now)

    def test_wrong_origin_replay_and_deleted_passkeys_are_rejected(self):
        device = SoftAuthenticator()
        self.register(self.code_session(), device)
        with self.assertRaises(LoginError):
            self.passkey_login(device, origin="https://evil.example.com")
        flow, options = self.login.login_options(RP_ID)
        assertion = device.get(json.loads(options))
        self.login.login(flow, assertion, RP_ID, ORIGIN)
        with self.assertRaises(LoginError, msg="a challenge works only once"):
            self.login.login(flow, assertion, RP_ID, ORIGIN)
        cid = self.login.passkeys_of("mumble:7")[0]["id"]
        self.assertFalse(self.login.delete_passkey("mumble:9", cid), "only your own passkeys")
        self.assertTrue(self.login.delete_passkey("mumble:7", cid))
        with self.assertRaisesRegex(LoginError, "kenne ich nicht"):
            self.passkey_login(device)

    def test_unknown_device_is_rejected(self):
        self.register(self.code_session(), SoftAuthenticator())
        with self.assertRaises(LoginError):
            self.passkey_login(SoftAuthenticator())

    def test_labels(self):
        self.assertEqual(device_label("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"), "Windows · Chrome")
        self.assertEqual(device_label("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
                                      "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"), "iPhone · Safari")


async def inner(scope, receive, send):
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        return
    body = json.dumps({"user": scope.get("web_user")}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


def call(app, path="/", headers=None, kind="http", method="GET"):
    sent = []

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    scope = {"type": kind, "path": path, "method": method, "scheme": "http",
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    asyncio.run(app(scope, receive, send))
    return sent


class GateTest(LoginTestCase):
    def test_without_session_only_the_login_page_is_reachable(self):
        gate = LoginGate(inner, self.login)
        start = call(gate, "/")[0]
        self.assertEqual((start["status"], dict(start["headers"])[b"location"]), (303, b"/login.html"))
        self.assertEqual(call(gate, "/api/state")[0]["status"], 401)
        self.assertEqual(call(gate, "/ws", kind="websocket")[0], {"type": "websocket.close", "code": 4401})
        for path in ("/login.html", "/login.js", "/passkey.js", "/style.css", "/healthz", "/auth/status"):
            self.assertEqual(call(gate, path)[0]["status"], 200, path)

    def test_the_old_shared_password_no_longer_works(self):
        gate = LoginGate(inner, self.login)
        basic = "Basic " + base64.b64encode(b"x:geheim").decode()
        self.assertEqual(call(gate, "/api/state", {"Authorization": basic})[0]["status"], 401)

    def test_session_cookie_lets_through_and_names_the_person(self):
        gate = LoginGate(inner, self.login)
        token = self.code_session()
        sent = call(gate, "/api/state", {"Cookie": f"other=1; {SESSION_COOKIE}={token}"})
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(json.loads(sent[1]["body"]), {"user": "Zasa"})
        self.assertEqual(call(gate, "/ws", {"Cookie": f"{SESSION_COOKIE}={token}"}, kind="websocket")[0]["type"],
                         "websocket.accept")


class ChatCommandTest(unittest.TestCase):
    def run_login(self, board, text: str, account: str = "mumble:7", private: bool = False):
        public, secret = [], []
        msg = IncomingText(sender="Zasa", sender_id="1", text=text, reply=public.append, private=private,
                           account=account, reply_private=None if private else secret.append)
        asyncio.run(board.commands.handle(msg))
        return public, secret

    def test_login_in_the_channel_answers_privately(self):
        from tests.test_commands import make_board

        with tempfile.TemporaryDirectory() as tmp:
            board = make_board(Path(tmp))
            board.web_login = WebLogin(Path(tmp) / "anmeldung.json", "Mumble")
            flow, code = board.web_login.start_code()
            public, secret = self.run_login(board, f"/login {code}")
            self.assertEqual(public, [], "nothing about the login in the channel")
            self.assertIn("freigegeben für <b>Zasa</b>", secret[0])
            self.assertEqual(board.web_login.poll_code(flow)[0], "ok")
            self.assertIn("hat sich im Webinterface angemeldet", board.activity[-1].text)
            _, secret = self.run_login(board, "/login AAAA-AAAA")
            self.assertIn("kenne ich nicht", secret[0])

    def test_without_public_web_interface_no_login_is_needed(self):
        from tests.test_commands import make_board

        with tempfile.TemporaryDirectory() as tmp:
            board = make_board(Path(tmp))
            self.assertIsNone(board.web_login)
            public, _ = self.run_login(board, "/login ABCD-EFGH", private=True)
            self.assertIn("braucht keine Anmeldung", public[0])

    def test_mumble_accounts(self):
        self.assertEqual(mumble_account(42, "Zasa"), "mumble:42")
        self.assertEqual(mumble_account(None, "Gast"), "mumble-name:gast")
        self.assertEqual(mumble_account(-1, "Gast"), "mumble-name:gast")


class MidnightTest(unittest.TestCase):
    def test_next_midnight_is_local(self):
        now = datetime(2026, 10, 13, 23, 59).astimezone().timestamp()
        midnight = datetime.fromtimestamp(next_midnight(now)).astimezone()
        self.assertEqual((midnight.day, midnight.hour, midnight.minute), (14, 0, 0))


if __name__ == "__main__":
    unittest.main()


def asgi(app, method: str, path: str, body: dict | None = None, cookie: str = "") -> tuple[int, dict, dict]:
    """One request against the real Starlette app (routes and LoginGate), without a server."""
    sent = []
    payload = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        sent.append(message)

    headers = [(b"host", b"localhost:8080"), (b"content-type", b"application/json")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": headers, "scheme": "http", "server": ("localhost", 8080), "client": ("127.0.0.1", 1),
             "http_version": "1.1", "root_path": ""}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    hdrs: dict = {}
    for k, v in start["headers"]:
        hdrs.setdefault(k.decode(), []).append(v.decode())
    return start["status"], json.loads(data or b"{}"), hdrs


class WebRoutesTest(unittest.TestCase):
    def test_code_login_through_the_web_app(self):
        from app.web import create_app
        from tests.test_commands import make_board

        with tempfile.TemporaryDirectory() as tmp:
            board = make_board(Path(tmp))
            board.web_login = WebLogin(Path(tmp) / "anmeldung.json", "Mumble")
            app = create_app(board)
            self.assertEqual(asgi(app, "GET", "/api/state")[0], 401)
            status, started, _ = asgi(app, "POST", "/auth/code")
            self.assertEqual(status, 200)
            self.assertEqual(asgi(app, "POST", "/auth/code/poll", {"flow": started["flow"]})[1], {"state": "pending"})
            board.web_login.confirm_code(started["code"], "mumble:7", "Zasa")
            status, polled, headers = asgi(app, "POST", "/auth/code/poll", {"flow": started["flow"]})
            self.assertEqual(polled, {"state": "ok"})
            cookie = headers["set-cookie"][0]
            self.assertIn(f"{SESSION_COOKIE}=", cookie)
            for flag in ("HttpOnly", "SameSite=lax", "Path=/"):
                self.assertIn(flag, cookie)
            session = cookie.split(";", 1)[0]
            self.assertEqual(asgi(app, "GET", "/auth/status", cookie=session)[1]["user"], "Zasa")
            self.assertEqual(asgi(app, "GET", "/api/state", cookie=session)[0], 200)
            seen = []
            board.stop_all = lambda *, requested_by, source: seen.append((requested_by, source)) or 0
            self.assertEqual(asgi(app, "POST", "/api/stop", cookie=session)[0], 200)
            self.assertEqual(seen, [("Zasa", "web")], "a web action carries the person's name")
            self.assertEqual(asgi(app, "POST", "/auth/logout", cookie=session)[0], 200)
            self.assertEqual(asgi(app, "GET", "/api/state", cookie=session)[0], 401)

    def test_locally_everything_stays_open(self):
        from app.web import create_app
        from tests.test_commands import make_board

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(make_board(Path(tmp)))
            self.assertEqual(asgi(app, "GET", "/api/state")[0], 200)
            self.assertEqual(asgi(app, "GET", "/auth/status")[1], {"enabled": False})
            self.assertEqual(asgi(app, "POST", "/auth/code")[0], 404)
