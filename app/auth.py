"""Password protection for the web interface when it is reachable from the internet.

HTTP Basic auth with any user name and the configured password. After a successful login the browser
also gets a cookie, so the WebSocket (which not every browser sends Basic credentials for) works too.
"/healthz" stays open for Kubernetes probes and the Mumble <-> Discord switch.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
from http.cookies import SimpleCookie

log = logging.getLogger("web")

COOKIE = "soundboard_auth"
OPEN_PATHS = {"/healthz"}
FAILURE_DELAY = 1.0  # slows down password guessing


def _token(password: str) -> str:
    """Cookie value: changes with the password, reveals nothing about it."""
    return hmac.new(password.encode(), b"soundboard-web-v1", hashlib.sha256).hexdigest()


def _headers(scope: dict) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


class BasicAuth:
    def __init__(self, app, password: str, realm: str = "Soundboard") -> None:
        self.app = app
        self.password = password
        self.token = _token(password)
        self.realm = realm

    def _basic_ok(self, header: str) -> bool:
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        _, sep, password = decoded.partition(":")
        return bool(sep) and hmac.compare_digest(password.encode(), self.password.encode())

    def _cookie_ok(self, header: str) -> bool:
        cookie = SimpleCookie()
        try:
            cookie.load(header)
        except Exception:  # noqa: BLE001 - a broken cookie header is just "not logged in"
            return False
        morsel = cookie.get(COOKIE)
        return morsel is not None and hmac.compare_digest(morsel.value.encode(), self.token.encode())

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket") or scope.get("path") in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        by_cookie = self._cookie_ok(headers.get("cookie", ""))
        by_password = not by_cookie and self._basic_ok(headers.get("authorization", ""))
        if not (by_cookie or by_password):
            await asyncio.sleep(FAILURE_DELAY)
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401})
                return
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", f'Basic realm="{self.realm}", charset="UTF-8"'.encode()),
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                ],
            })
            await send({"type": "http.response.body", "body": "Passwort erforderlich".encode()})
            return
        if not by_password or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        secure = scope.get("scheme") == "https" or headers.get("x-forwarded-proto", "") == "https"
        cookie = f"{COOKIE}={self.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"
        if secure:
            cookie += "; Secure"

        async def send_with_cookie(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"set-cookie", cookie.encode())]}
            await send(message)

        await self.app(scope, receive, send_with_cookie)
