"""Mumble wire format helpers: TCP framing, legacy varints and voice packets.

References:
https://mumble-protocol.readthedocs.io/en/latest/
The legacy (pre-1.5) voice format is used because every server version understands it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from . import Mumble_pb2 as pb

PROTOCOL_VERSION = (1, 4, 0)


def version_v1(major: int, minor: int, patch: int) -> int:
    return (major << 16) | (minor << 8) | min(patch, 255)


def version_v2(major: int, minor: int, patch: int) -> int:
    return (major << 48) | (minor << 32) | (patch << 16)


# Message type ids as defined by the Mumble protocol.
MESSAGE_TYPES: list[type] = [
    pb.Version,  # 0
    pb.UDPTunnel,  # 1
    pb.Authenticate,  # 2
    pb.Ping,  # 3
    pb.Reject,  # 4
    pb.ServerSync,  # 5
    pb.ChannelRemove,  # 6
    pb.ChannelState,  # 7
    pb.UserRemove,  # 8
    pb.UserState,  # 9
    pb.BanList,  # 10
    pb.TextMessage,  # 11
    pb.PermissionDenied,  # 12
    pb.ACL,  # 13
    pb.QueryUsers,  # 14
    pb.CryptSetup,  # 15
    pb.ContextActionModify,  # 16
    pb.ContextAction,  # 17
    pb.UserList,  # 18
    pb.VoiceTarget,  # 19
    pb.PermissionQuery,  # 20
    pb.CodecVersion,  # 21
    pb.UserStats,  # 22
    pb.RequestBlob,  # 23
    pb.ServerConfig,  # 24
    pb.SuggestConfig,  # 25
    pb.PluginDataTransmission,  # 26
]
TYPE_IDS: dict[type, int] = {cls: i for i, cls in enumerate(MESSAGE_TYPES)}
UDP_TUNNEL = 1
HEADER = struct.Struct(">HI")
MAX_MESSAGE_SIZE = 8 * 1024 * 1024

# Legacy UDP voice packet types (upper three bits of the header byte).
VOICE_CELT_ALPHA = 0
VOICE_PING = 1
VOICE_SPEEX = 2
VOICE_CELT_BETA = 3
VOICE_OPUS = 4
OPUS_TERMINATOR = 0x2000


def pack_message(msg) -> bytes:
    payload = msg.SerializeToString()
    return HEADER.pack(TYPE_IDS[type(msg)], len(payload)) + payload


def pack_tunnel(packet: bytes) -> bytes:
    return HEADER.pack(UDP_TUNNEL, len(packet)) + packet


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("Negative varints werden nicht benötigt")
    if value < 0x80:
        return bytes([value])
    if value < 0x4000:
        return bytes([0x80 | (value >> 8), value & 0xFF])
    if value < 0x200000:
        return bytes([0xC0 | (value >> 16), (value >> 8) & 0xFF, value & 0xFF])
    if value < 0x10000000:
        return bytes([0xE0 | (value >> 24), (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF])
    if value < 0x100000000:
        return bytes([0xF0]) + struct.pack(">I", value)
    return bytes([0xF4]) + struct.pack(">Q", value)


def decode_varint(data: bytes, pos: int = 0) -> tuple[int, int]:
    """Return (value, new_position)."""
    b = data[pos]
    if b & 0x80 == 0x00:
        return b & 0x7F, pos + 1
    if b & 0xC0 == 0x80:
        return ((b & 0x3F) << 8) | data[pos + 1], pos + 2
    if b & 0xE0 == 0xC0:
        return ((b & 0x1F) << 16) | (data[pos + 1] << 8) | data[pos + 2], pos + 3
    if b & 0xF0 == 0xE0:
        return ((b & 0x0F) << 24) | (data[pos + 1] << 16) | (data[pos + 2] << 8) | data[pos + 3], pos + 4
    if b & 0xFC == 0xF0:
        return struct.unpack_from(">I", data, pos + 1)[0], pos + 5
    if b & 0xFC == 0xF4:
        return struct.unpack_from(">Q", data, pos + 1)[0], pos + 9
    if b & 0xFC == 0xF8:
        value, pos = decode_varint(data, pos + 1)
        return -value, pos
    if b & 0xFC == 0xFC:
        return -(b & 0x03), pos + 1
    raise ValueError("Ungültiger varint")


def build_opus_packet(opus: bytes, sequence: int, terminator: bool, target: int = 0) -> bytes:
    header = (VOICE_OPUS << 5) | (target & 0x1F)
    size = len(opus) | (OPUS_TERMINATOR if terminator else 0)
    return bytes([header]) + encode_varint(sequence) + encode_varint(size) + opus


@dataclass
class VoicePacket:
    codec: int
    target: int
    session: int
    sequence: int
    opus: bytes
    terminator: bool


def parse_server_voice_packet(packet: bytes) -> VoicePacket | None:
    """Parse a legacy voice packet as relayed by the server (contains the sender session)."""
    if not packet:
        return None
    codec, target = packet[0] >> 5, packet[0] & 0x1F
    if codec != VOICE_OPUS:
        return None
    session, pos = decode_varint(packet, 1)
    sequence, pos = decode_varint(packet, pos)
    size, pos = decode_varint(packet, pos)
    terminator = bool(size & OPUS_TERMINATOR)
    size &= 0x1FFF
    return VoicePacket(codec, target, session, sequence, packet[pos : pos + size], terminator)


class ServerRateLimit:
    """Model of the server's message rate limit, used to pace outgoing text and state messages.

    Murmur drops UserState/TextMessage messages over "messagelimit" per second with a burst of
    "messageburst" (defaults 1 and 5), silently. Its LeakyBucket (1.3) drains only whole tokens and
    restarts its timer on every limited message, so time between messages closer than one second
    apart is lost. This model mirrors that behaviour (newer servers are more lenient) with a safety
    margin for network jitter.
    """

    def __init__(self, rate: float = 1.0, burst: int = 5, *, margin: float = 0.15, reserve: int = 1) -> None:
        self.rate = rate
        self.burst = burst - reserve  # leave room for messages we cannot account for exactly
        self.margin = margin
        self.current = 0
        self.last = -1e9

    def _drained(self, now: float) -> int:
        # The server may see the gap up to `margin` shorter (jitter), so only count time it surely saw.
        drain = int(max(0.0, now - self.last - self.margin) * self.rate + 1e-9)
        return max(0, self.current - drain)

    def wait_time(self, now: float) -> float:
        """Seconds to wait before the next message may be sent (0 = now)."""
        if self._drained(now) + 1 <= self.burst:
            return 0.0
        needed = self.current + 1 - self.burst  # whole tokens that must drain first
        return max(0.0, needed / self.rate + self.margin - (now - self.last))

    def consume(self, now: float) -> None:
        self.current = self._drained(now) + 1
        self.last = now
