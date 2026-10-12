"""Asyncio Mumble client: control channel over TLS, voice tunnelled through TCP.

Only what a soundboard bot needs is implemented: state tracking for users and channels,
text messages, moving between channels and sending Opus voice packets.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from . import Mumble_pb2 as pb
from .protocol import (
    HEADER,
    MAX_MESSAGE_SIZE,
    MESSAGE_TYPES,
    PROTOCOL_VERSION,
    UDP_TUNNEL,
    ServerRateLimit,
    VoicePacket,
    build_opus_packet,
    pack_message,
    pack_tunnel,
    parse_server_voice_packet,
    version_v1,
    version_v2,
)

log = logging.getLogger("mumble")

PING_INTERVAL = 10.0
READ_TIMEOUT = 45.0
CONNECT_TIMEOUT = 15.0
# Rough per-packet overhead (IP/UDP/crypt/varints) used to derive the usable Opus bitrate,
# mirrors the calculation of the official client for 20 ms frames.
VOICE_OVERHEAD_BPS = 20000


@dataclass
class Channel:
    id: int
    name: str = ""
    parent: int | None = None
    position: int = 0


@dataclass
class User:
    session: int
    name: str = ""
    channel_id: int = 0
    user_id: int | None = None
    self_mute: bool = False
    self_deaf: bool = False
    mute: bool = False
    deaf: bool = False


@dataclass
class TextMessage:
    actor: int | None
    sender: str
    message: str
    channel_ids: list[int] = field(default_factory=list)
    sessions: list[int] = field(default_factory=list)
    tree_ids: list[int] = field(default_factory=list)

    @property
    def is_private(self) -> bool:
        return bool(self.sessions) and not self.channel_ids and not self.tree_ids


class RejectedError(ConnectionError):
    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(f"Server hat die Verbindung abgelehnt: {kind} {reason}".strip())
        self.kind = kind
        self.reason = reason


class Listener(Protocol):
    async def on_ready(self) -> None: ...
    async def on_disconnected(self, reason: str) -> None: ...
    async def on_text_message(self, msg: TextMessage) -> None: ...
    async def on_user_changed(self, user: User, old: User | None) -> None: ...
    async def on_user_removed(self, user: User) -> None: ...
    async def on_permission_denied(self, text: str) -> None: ...
    def on_voice(self, packet: VoicePacket) -> None: ...


class MumbleClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str = "",
        tokens: list[str] | None = None,
        certfile: str | None = None,
        keyfile: str | None = None,
        listener: Listener,
        client_type: int = 1,
        release: str = "claudemumble soundboard",
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.tokens = tokens or []
        self.certfile = certfile
        self.keyfile = keyfile
        self.listener = listener
        self.client_type = client_type
        self.release = release

        self.channels: dict[int, Channel] = {}
        self.users: dict[int, User] = {}
        self.session: int | None = None
        self.max_bandwidth: int | None = None
        self.message_length = 5000
        self.allow_html = True
        self.server_release = ""
        self.opus_supported = True
        self.last_error = ""
        self.connected_since: float | None = None

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ready = asyncio.Event()
        self._closing = False
        self._sequence = 0
        self._last_voice = 0.0
        self._outbox: deque[tuple[str, object]] = deque()
        self._outbox_event = asyncio.Event()
        self._rate_limit = ServerRateLimit()

    # ------------------------------------------------------------------ state

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self._writer is not None and not self._writer.is_closing()

    @property
    def myself(self) -> User | None:
        return self.users.get(self.session) if self.session is not None else None

    @property
    def my_channel(self) -> Channel | None:
        me = self.myself
        return self.channels.get(me.channel_id) if me else None

    def channel_path(self, channel_id: int) -> str:
        parts: list[str] = []
        seen: set[int] = set()
        cid: int | None = channel_id
        while cid is not None and cid in self.channels and cid not in seen:
            seen.add(cid)
            ch = self.channels[cid]
            if ch.parent is None:
                break  # root channel name is the server name, leave it out
            parts.append(ch.name)
            cid = ch.parent
        return "/".join(reversed(parts)) or (self.channels[channel_id].name if channel_id in self.channels else "")

    def find_channel(self, path: str) -> Channel | None:
        """Resolve "Parent/Child" paths or a unique channel name (case-insensitive)."""
        path = path.strip().strip("/")
        if not path:
            return self.channels.get(0)
        if "/" in path:
            current = self.channels.get(0)
            for part in path.split("/"):
                if current is None:
                    return None
                children = [c for c in self.channels.values() if c.parent == current.id]
                current = next((c for c in children if c.name == part), None) or next(
                    (c for c in children if c.name.casefold() == part.casefold()), None
                )
            return current
        exact = [c for c in self.channels.values() if c.name == path]
        if exact:
            return exact[0]
        folded = [c for c in self.channels.values() if c.name.casefold() == path.casefold()]
        return folded[0] if folded else None

    def find_user(self, name: str) -> User | None:
        folded = name.casefold()
        return next((u for u in self.users.values() if u.name.casefold() == folded), None)

    def users_in_channel(self, channel_id: int) -> list[User]:
        return sorted(
            (u for u in self.users.values() if u.channel_id == channel_id and u.session != self.session),
            key=lambda u: u.name.casefold(),
        )

    def usable_bitrate(self, wanted: int) -> int:
        if not self.max_bandwidth:
            return wanted
        return max(8000, min(wanted, self.max_bandwidth - VOICE_OVERHEAD_BPS))

    # ------------------------------------------------------------- lifecycle

    async def run_forever(self) -> None:
        backoff = 1.0
        while not self._closing:
            started = time.monotonic()
            reason = "Verbindung beendet"
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                raise
            except RejectedError as exc:
                reason = str(exc)
                backoff = max(backoff, 10.0)
            except asyncio.IncompleteReadError:
                reason = "Server hat die Verbindung geschlossen"
            except (TimeoutError, asyncio.TimeoutError):
                reason = "Zeitüberschreitung bei der Verbindung"
            except (OSError, ssl.SSLError, ConnectionError, ValueError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            finally:
                await self._cleanup()
            self.last_error = reason
            log.warning("Mumble getrennt: %s", reason)
            try:
                await self.listener.on_disconnected(reason)
            except Exception:  # noqa: BLE001 - listener errors must not kill the loop
                log.exception("Fehler im Disconnect-Handler")
            if self._closing:
                break
            if time.monotonic() - started > 60:
                backoff = 1.0
            log.info("Neuer Verbindungsversuch in %.0f s", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def close(self) -> None:
        self._closing = True
        await self._cleanup()

    async def _cleanup(self) -> None:
        self._ready.clear()
        self._outbox.clear()
        writer, self._writer, self._reader = self._writer, None, None
        self.session = None
        self.connected_since = None
        self.users.clear()
        self.channels.clear()
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except Exception:  # noqa: BLE001 - socket is gone anyway
                pass

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Mumble servers almost always use self-signed certificates.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if self.certfile and self.keyfile:
            ctx.load_cert_chain(self.certfile, self.keyfile)
        return ctx

    async def _connect_and_serve(self) -> None:
        log.info("Verbinde zu %s:%s als %s", self.host, self.port, self.username)
        self._rate_limit = ServerRateLimit()
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=self._ssl_context()), CONNECT_TIMEOUT
        )
        major, minor, patch = PROTOCOL_VERSION
        self._send(
            pb.Version(
                version_v1=version_v1(major, minor, patch),
                version_v2=version_v2(major, minor, patch),
                release=self.release,
                os=platform.system(),
                os_version=platform.release(),
            )
        )
        auth = pb.Authenticate(username=self.username, password=self.password, opus=True)
        auth.tokens.extend(self.tokens)
        auth.client_type = self.client_type
        self._send(auth)

        ping_task = asyncio.create_task(self._ping_loop(), name="mumble-ping")
        outbox_task = asyncio.create_task(self._outbox_loop(), name="mumble-outbox")
        try:
            while True:
                header = await asyncio.wait_for(self._reader.readexactly(HEADER.size), READ_TIMEOUT)
                msg_type, length = HEADER.unpack(header)
                if length > MAX_MESSAGE_SIZE:
                    raise ValueError(f"Nachricht zu gross ({length} Bytes)")
                payload = await asyncio.wait_for(self._reader.readexactly(length), READ_TIMEOUT)
                await self._dispatch(msg_type, payload)
        finally:
            ping_task.cancel()
            outbox_task.cancel()

    async def _ping_loop(self) -> None:
        while self._writer is not None and not self._writer.is_closing():
            self._send(pb.Ping(timestamp=int(time.time() * 1000)))
            await asyncio.sleep(PING_INTERVAL)

    # -------------------------------------------------------------- receive

    async def _dispatch(self, msg_type: int, payload: bytes) -> None:
        if msg_type == UDP_TUNNEL:
            packet = parse_server_voice_packet(payload)
            on_voice = getattr(self.listener, "on_voice", None)
            if packet and on_voice:
                on_voice(packet)
            return
        if msg_type >= len(MESSAGE_TYPES):
            return
        msg = MESSAGE_TYPES[msg_type].FromString(payload)
        handler = getattr(self, f"_on_{type(msg).__name__}", None)
        if handler:
            await handler(msg)

    async def _on_Version(self, msg: pb.Version) -> None:  # noqa: N802
        self.server_release = msg.release

    async def _on_Reject(self, msg: pb.Reject) -> None:  # noqa: N802
        kind = pb.Reject.RejectType.Name(msg.type) if msg.HasField("type") else ""
        raise RejectedError(kind, msg.reason)

    async def _on_ServerSync(self, msg: pb.ServerSync) -> None:  # noqa: N802
        self.session = msg.session
        self.max_bandwidth = msg.max_bandwidth if msg.HasField("max_bandwidth") else None
        self.connected_since = time.time()
        self.last_error = ""
        self._ready.set()
        me = self.myself
        log.info(
            "Verbunden mit %s (Server %s), Session %s, Kanal %s",
            self.host,
            self.server_release or "?",
            self.session,
            self.channel_path(me.channel_id) if me else "?",
        )
        await self.listener.on_ready()

    async def _on_ServerConfig(self, msg: pb.ServerConfig) -> None:  # noqa: N802
        if msg.HasField("message_length") and msg.message_length > 0:
            self.message_length = msg.message_length
        if msg.HasField("allow_html"):
            self.allow_html = msg.allow_html
        if msg.HasField("max_bandwidth"):
            self.max_bandwidth = msg.max_bandwidth

    async def _on_CodecVersion(self, msg: pb.CodecVersion) -> None:  # noqa: N802
        self.opus_supported = msg.opus
        if not msg.opus:
            log.warning("Server meldet, dass nicht alle Clients Opus können - Audio evtl. unhörbar")

    async def _on_ChannelState(self, msg: pb.ChannelState) -> None:  # noqa: N802
        ch = self.channels.setdefault(msg.channel_id, Channel(id=msg.channel_id))
        if msg.HasField("name"):
            ch.name = msg.name
        if msg.HasField("parent"):
            ch.parent = msg.parent
        if msg.HasField("position"):
            ch.position = msg.position

    async def _on_ChannelRemove(self, msg: pb.ChannelRemove) -> None:  # noqa: N802
        self.channels.pop(msg.channel_id, None)

    async def _on_UserState(self, msg: pb.UserState) -> None:  # noqa: N802
        existing = self.users.get(msg.session)
        old = User(**vars(existing)) if existing else None
        user = existing or User(session=msg.session)
        for attr in ("name", "channel_id", "user_id", "self_mute", "self_deaf", "mute", "deaf"):
            if msg.HasField(attr):
                setattr(user, attr, getattr(msg, attr))
        self.users[msg.session] = user
        if self._ready.is_set():
            await self.listener.on_user_changed(user, old)

    async def _on_UserRemove(self, msg: pb.UserRemove) -> None:  # noqa: N802
        user = self.users.pop(msg.session, None)
        if msg.session == self.session:
            raise ConnectionError(f"Vom Server entfernt: {msg.reason or 'kein Grund angegeben'}")
        if user and self._ready.is_set():
            await self.listener.on_user_removed(user)

    async def _on_TextMessage(self, msg: pb.TextMessage) -> None:  # noqa: N802
        actor = msg.actor if msg.HasField("actor") else None
        sender = self.users[actor].name if actor in self.users else ""
        await self.listener.on_text_message(
            TextMessage(
                actor=actor,
                sender=sender,
                message=msg.message,
                channel_ids=list(msg.channel_id),
                sessions=list(msg.session),
                tree_ids=list(msg.tree_id),
            )
        )

    async def _on_PermissionDenied(self, msg: pb.PermissionDenied) -> None:  # noqa: N802
        kind = pb.PermissionDenied.DenyType.Name(msg.type) if msg.HasField("type") else "Permission"
        text = msg.reason or kind
        log.warning("Mumble verweigert: %s", text)
        await self.listener.on_permission_denied(text)

    # ----------------------------------------------------------------- send

    def _send(self, msg) -> None:
        """Send immediately (pings, handshake). Rate limited messages go through the outbox."""
        if self._writer is None or self._writer.is_closing():
            raise ConnectionError("Nicht mit Mumble verbunden")
        self._writer.write(pack_message(msg))

    def _enqueue(self, kind: str, msg) -> None:
        """Queue a rate limited control message. A newer message of a coalescing kind replaces the old one."""
        if self._writer is None or self._writer.is_closing():
            raise ConnectionError("Nicht mit Mumble verbunden")
        if kind in ("move", "comment"):
            self._outbox = deque(item for item in self._outbox if item[0] != kind)
        self._outbox.append((kind, msg))
        self._outbox_event.set()

    @property
    def pending_move(self) -> bool:
        return any(kind == "move" for kind, _ in self._outbox)

    async def _outbox_loop(self) -> None:
        while True:
            await self._outbox_event.wait()
            self._outbox_event.clear()
            while self._outbox:
                wait = self._rate_limit.wait_time(time.monotonic())
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue
                _, msg = self._outbox.popleft()
                self._send(msg)
                self._rate_limit.consume(time.monotonic())

    def send_text(self, html: str, *, channel_id: int | None = None, session: int | None = None) -> None:
        msg = pb.TextMessage(message=html)
        if session is not None:
            msg.session.append(session)
        elif channel_id is not None:
            msg.channel_id.append(channel_id)
        else:
            raise ValueError("Ziel für die Textnachricht fehlt")
        self._enqueue("text", msg)

    def move_to(self, channel_id: int) -> None:
        if self.session is None:
            raise ConnectionError("Nicht mit Mumble verbunden")
        self._enqueue("move", pb.UserState(session=self.session, channel_id=channel_id))

    def set_comment(self, comment: str) -> None:
        if self.session is None:
            raise ConnectionError("Nicht mit Mumble verbunden")
        self._enqueue("comment", pb.UserState(session=self.session, comment=comment))

    def send_voice(self, opus: bytes, terminator: bool = False) -> None:
        """Send one 20 ms Opus frame through the TCP tunnel."""
        if not self.ready:
            return
        now = time.monotonic()
        if now - self._last_voice > 5.0:
            self._sequence = 0  # new utterance after a pause, like the official client
        self._last_voice = now
        assert self._writer is not None
        self._writer.write(pack_tunnel(build_opus_packet(opus, self._sequence, terminator)))
        self._sequence += 2  # sequence counts 10 ms units

    @property
    def write_buffer_size(self) -> int:
        if self._writer is None or self._writer.transport is None:
            return 0
        return self._writer.transport.get_write_buffer_size()

    async def drain(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            try:
                await self._writer.drain()
            except (ConnectionError, OSError):
                pass  # the read loop notices the broken connection and reconnects
