"""Login for the web interface when it is reachable from the internet (WEB_PASSWORD is set).

Works like the OAuth device flow: the login page shows a short code, the person sends it to the bot in
the chat (Mumble or Discord, "/login K7Q2-M9XD"), the bot knows who wrote it and unlocks exactly the
browser that asked for the code. Right after that the page pushes for a passkey: with one, a session
lasts 30 days and the next login needs no chat at all; without one it ends at midnight.

The shared password itself is no longer accepted; it only marks "this instance is public" (the Helm
chart sets it). Without WEB_PASSWORD - the local setup - the web interface stays open as before.

Accounts, passkeys and sessions live in /data/anmeldung.json so that restarts (e.g. for a new yt-dlp)
log nobody out. Pending codes and WebAuthn challenges are short-lived and stay in memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path

log = logging.getLogger("web")

SESSION_COOKIE = "bard_session"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: easy to type from the screen
CODE_LENGTH = 8
CODE_TTL = 5 * 60
CHALLENGE_TTL = 5 * 60
PASSKEY_SESSION_DAYS = 30
MAX_FAILURES = 10  # wrong codes per person ...
FAILURE_WINDOW = 10 * 60  # ... within this time
MAX_PENDING = 500  # open login attempts kept in memory (the login page is public)

# Reachable without a session: the login page, its files and the login API. Everything else needs one.
OPEN_PATHS = {"/healthz", "/login.html", "/login.js", "/passkey.js", "/style.css", "/favicon.svg"}
OPEN_PREFIX = "/auth/"


class LoginError(Exception):
    """Shown to the person as it is (German)."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_code(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def next_midnight(now: float) -> float:
    local = datetime.fromtimestamp(now).astimezone()
    return (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


def device_label(user_agent: str) -> str:
    """A rough "Windows · Chrome" so people can tell their passkeys apart."""
    ua = user_agent or ""
    system = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                          ("Windows", "Windows"), ("Mac OS X", "Mac"), ("Linux", "Linux"))
                   if key in ua), "")
    browser = next((name for key, name in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                                           ("Safari/", "Safari")) if key in ua), "")
    return " · ".join(p for p in (system, browser) if p) or "Unbekanntes Gerät"


@dataclass
class Pending:
    code: str
    expires: float
    account: str | None = None
    created: float = 0.0


class LoginStore:
    """anmeldung.json: accounts (name, WebAuthn user handle), passkeys and sessions."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.accounts: dict[str, dict] = {}
        self.passkeys: list[dict] = []
        self.sessions: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # Keep the broken file for inspection instead of silently overwriting everyone's passkeys.
            log.error("Anmeldedaten %s nicht lesbar (%s), beginne leer", self.path, exc)
            try:
                self.path.replace(self.path.with_suffix(".kaputt.json"))
            except OSError:
                pass
            return
        self.accounts = dict(data.get("accounts") or {})
        self.passkeys = list(data.get("passkeys") or [])
        self.sessions = dict(data.get("sessions") or {})

    def save(self) -> None:
        now = time.time()
        self.sessions = {h: s for h, s in self.sessions.items() if s.get("expires", 0) > now}
        data = {"version": 1, "accounts": self.accounts, "passkeys": self.passkeys, "sessions": self.sessions}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            os.chmod(tmp, 0o600)
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


class WebLogin:
    def __init__(self, path: Path, platform: str, clock=time.time) -> None:
        self.store = LoginStore(path)
        self.platform = platform  # "Mumble" or "Discord": named on the login page and in passkeys
        self.clock = clock
        self._pending: dict[str, Pending] = {}  # flow token -> pending code
        self._challenges: dict[str, tuple[bytes, float]] = {}  # flow token or session hash -> challenge
        self._failures: dict[str, deque[float]] = {}

    # ----------------------------------------------------------------- codes (device flow)

    def _prune(self) -> None:
        now = self.clock()
        self._pending = {f: p for f, p in self._pending.items() if p.expires > now}
        self._challenges = {k: c for k, c in self._challenges.items() if c[1] > now}

    def start_code(self) -> tuple[str, str]:
        """A new login attempt from a browser: (flow token for that browser, code to send in the chat)."""
        self._prune()
        if len(self._pending) >= MAX_PENDING:
            oldest = min(self._pending, key=lambda f: self._pending[f].created)
            del self._pending[oldest]
        taken = {p.code for p in self._pending.values()}
        while (code := "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))) in taken:
            pass
        flow = secrets.token_urlsafe(32)
        now = self.clock()
        self._pending[flow] = Pending(code=code, expires=now + CODE_TTL, created=now)
        return flow, format_code(code)

    def confirm_code(self, text: str, account: str, name: str) -> None:
        """The person `account` sent `text` in the chat. Raises LoginError with the reason."""
        now = self.clock()
        failures = self._failures.setdefault(account, deque())
        while failures and failures[0] < now - FAILURE_WINDOW:
            failures.popleft()
        if len(failures) >= MAX_FAILURES:
            raise LoginError("Zu viele falsche Codes, bitte warte ein paar Minuten.")
        code = normalize_code(text)
        self._prune()
        pending = next((p for p in self._pending.values() if p.code == code and p.account is None), None)
        if pending is None:
            failures.append(now)
            raise LoginError("Diesen Code kenne ich nicht, oder er ist abgelaufen. "
                             "Lade die Anmeldeseite neu, dann gibt es einen neuen.")
        pending.account = account
        self._remember(account, name)

    def poll_code(self, flow: str) -> tuple[str, str | None]:
        """("pending" | "expired" | "ok", session token when ok). A confirmed code is used up."""
        self._prune()
        pending = self._pending.get(flow)
        if pending is None:
            return "expired", None
        if pending.account is None:
            return "pending", None
        del self._pending[flow]
        return "ok", self.new_session(pending.account, passkey=False)

    # ------------------------------------------------------------------------- sessions

    def _remember(self, account: str, name: str) -> None:
        entry = self.store.accounts.setdefault(account, {})
        entry["name"] = name
        entry.setdefault("handle", secrets.token_urlsafe(16))
        self.store.save()

    def name_of(self, account: str) -> str:
        return self.store.accounts.get(account, {}).get("name") or account

    def new_session(self, account: str, *, passkey: bool) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        expires = now + PASSKEY_SESSION_DAYS * 86400 if passkey else next_midnight(now)
        self.store.sessions[_hash(token)] = {"account": account, "expires": expires, "passkey": passkey}
        self.store.save()
        return token

    def session(self, token: str | None) -> dict | None:
        if not token:
            return None
        s = self.store.sessions.get(_hash(token))
        if s is None or s.get("expires", 0) <= self.clock():
            return None
        return s

    def end_session(self, token: str | None) -> None:
        if token and self.store.sessions.pop(_hash(token), None) is not None:
            self.store.save()

    # ------------------------------------------------------------------------- passkeys

    def passkeys_of(self, account: str) -> list[dict]:
        return [{"id": p["id"], "label": p.get("label", ""), "created": p.get("created"),
                 "last_used": p.get("last_used")}
                for p in self.store.passkeys if p["account"] == account]

    def delete_passkey(self, account: str, cid: str) -> bool:
        before = len(self.store.passkeys)
        self.store.passkeys = [p for p in self.store.passkeys if not (p["id"] == cid and p["account"] == account)]
        if len(self.store.passkeys) == before:
            return False
        self.store.save()
        return True

    def registration_options(self, token: str, rp_id: str) -> str:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers import base64url_to_bytes
        from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                              ResidentKeyRequirement, UserVerificationRequirement)

        s = self.session(token)
        if s is None:
            raise LoginError("Nicht angemeldet")
        account = s["account"]
        name = self.name_of(account)
        handle = self.store.accounts.setdefault(account, {"name": name}).setdefault("handle", secrets.token_urlsafe(16))
        options = generate_registration_options(
            rp_id=rp_id,
            rp_name=f"Bard-Board ({self.platform})",
            user_id=base64url_to_bytes(handle),
            user_name=name,
            user_display_name=f"{name} ({self.platform})",
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["id"]))
                                 for p in self.store.passkeys if p["account"] == account],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
            timeout=CHALLENGE_TTL * 1000,
        )
        self._challenges[_hash(token)] = (options.challenge, self.clock() + CHALLENGE_TTL)
        return options_to_json(options)

    def register(self, token: str, credential: dict, rp_id: str, origin: str, label: str) -> None:
        """Store the new passkey and turn the session into a 30-day one."""
        from webauthn import verify_registration_response
        from webauthn.helpers import bytes_to_base64url
        from webauthn.helpers.exceptions import WebAuthnException

        s = self.session(token)
        if s is None:
            raise LoginError("Nicht angemeldet")
        challenge = self._challenges.pop(_hash(token), None)
        if challenge is None or challenge[1] <= self.clock():
            raise LoginError("Zu lange gewartet, bitte nochmal versuchen.")
        try:
            verified = verify_registration_response(credential=credential, expected_challenge=challenge[0],
                                                    expected_rp_id=rp_id, expected_origin=origin)
        except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
            log.info("Passkey-Registrierung abgelehnt: %s", exc)
            raise LoginError("Der Passkey konnte nicht geprüft werden.") from exc
        cid = bytes_to_base64url(verified.credential_id)
        if any(p["id"] == cid for p in self.store.passkeys):
            raise LoginError("Diesen Passkey gibt es schon.")
        self.store.passkeys.append({
            "id": cid,
            "public_key": bytes_to_base64url(verified.credential_public_key),
            "sign_count": verified.sign_count,
            "account": s["account"],
            "label": label,
            "created": self.clock(),
            "last_used": None,
        })
        s["passkey"] = True
        s["expires"] = self.clock() + PASSKEY_SESSION_DAYS * 86400
        self.store.save()
        log.info("Passkey angelegt für %s (%s)", self.name_of(s["account"]), label)

    def login_options(self, rp_id: str) -> tuple[str, str]:
        """(flow token, options JSON) for "sign in with a passkey" (discoverable: no name needed)."""
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import UserVerificationRequirement

        self._prune()
        if len(self._challenges) >= MAX_PENDING:
            raise LoginError("Gerade zu viele Anmeldeversuche, bitte gleich nochmal.")
        options = generate_authentication_options(rp_id=rp_id, timeout=CHALLENGE_TTL * 1000,
                                                  user_verification=UserVerificationRequirement.PREFERRED)
        flow = secrets.token_urlsafe(32)
        self._challenges[flow] = (options.challenge, self.clock() + CHALLENGE_TTL)
        return flow, options_to_json(options)

    def login(self, flow: str, credential: dict, rp_id: str, origin: str) -> str:
        """Check a passkey assertion; returns a 30-day session token."""
        from webauthn import verify_authentication_response
        from webauthn.helpers import base64url_to_bytes
        from webauthn.helpers.exceptions import WebAuthnException

        challenge = self._challenges.pop(flow, None)
        if challenge is None or challenge[1] <= self.clock():
            raise LoginError("Zu lange gewartet, bitte nochmal versuchen.")
        cid = str(credential.get("id", "")) if isinstance(credential, dict) else ""
        passkey = next((p for p in self.store.passkeys if p["id"] == cid), None)
        if passkey is None:
            raise LoginError("Diesen Passkey kenne ich nicht (mehr). Melde dich über den Chat an.")
        try:
            verified = verify_authentication_response(
                credential=credential, expected_challenge=challenge[0], expected_rp_id=rp_id,
                expected_origin=origin, credential_public_key=base64url_to_bytes(passkey["public_key"]),
                credential_current_sign_count=int(passkey.get("sign_count", 0)))
        except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
            log.info("Passkey-Anmeldung abgelehnt: %s", exc)
            raise LoginError("Der Passkey konnte nicht geprüft werden.") from exc
        passkey["sign_count"] = verified.new_sign_count
        passkey["last_used"] = self.clock()
        return self.new_session(passkey["account"], passkey=True)


# ------------------------------------------------------------------------------ middleware


def _headers(scope: dict) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def session_token(headers: dict[str, str]) -> str | None:
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("cookie", ""))
    except Exception:  # noqa: BLE001 - a broken cookie header is just "not logged in"
        return None
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel is not None else None


class LoginGate:
    """Lets only signed-in browsers through; tells the app who it is (scope["web_user"])."""

    def __init__(self, app, login: WebLogin) -> None:
        self.app = app
        self.login = login

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = session_token(_headers(scope))
        session = self.login.session(token)
        if session is not None:
            scope["web_user"] = self.login.name_of(session["account"])
            scope["web_session"] = token
        path = scope.get("path", "")
        if session is not None or path in OPEN_PATHS or path.startswith(OPEN_PREFIX):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        if path in ("/", "/index.html") and scope.get("method", "GET") in ("GET", "HEAD"):
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", b"/login.html"), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b""})
            return
        body = json.dumps({"error": "Anmeldung erforderlich"}).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
