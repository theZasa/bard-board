"""Minimal ctypes binding for libopus (encoder and decoder)."""

from __future__ import annotations

import ctypes
import ctypes.util

SAMPLE_RATE = 48000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 960 samples per channel

_APPLICATION_AUDIO = 2049
_SET_BITRATE = 4002
_SET_VBR = 4006
_SET_COMPLEXITY = 4010
_SET_SIGNAL = 4024
_SIGNAL_MUSIC = 3002
_MAX_PACKET = 4000


class OpusError(RuntimeError):
    pass


def _load() -> ctypes.CDLL:
    candidates = [ctypes.util.find_library("opus"), "libopus.so.0", "libopus.so", "libopus.0.dylib", "opus.dll"]
    for name in candidates:
        if not name:
            continue
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise OpusError("libopus wurde nicht gefunden (Paket libopus0 installieren)")


_lib = _load()
_c_int_p = ctypes.POINTER(ctypes.c_int)

_lib.opus_strerror.restype = ctypes.c_char_p
_lib.opus_strerror.argtypes = [ctypes.c_int]
_lib.opus_encoder_create.restype = ctypes.c_void_p
_lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, _c_int_p]
_lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
_lib.opus_encode.restype = ctypes.c_int32
_lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int32]
_lib.opus_encoder_ctl.restype = ctypes.c_int
# opus_encoder_ctl is variadic. Every request used here takes exactly one int argument, so the
# signature can be declared. Without argtypes ctypes would pass the encoder pointer as a 32-bit int
# and truncate it (segfault as soon as the heap lives above 4 GiB, e.g. with a PIE Python).
_lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_lib.opus_get_version_string.restype = ctypes.c_char_p
_lib.opus_get_version_string.argtypes = []
_lib.opus_decoder_create.restype = ctypes.c_void_p
_lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, _c_int_p]
_lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
_lib.opus_decode.restype = ctypes.c_int
_lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]


def _check(code: int) -> int:
    if code < 0:
        raise OpusError(_lib.opus_strerror(code).decode())
    return code


def version() -> str:
    return _lib.opus_get_version_string().decode()


class Encoder:
    """Mono 48 kHz Opus encoder producing 20 ms packets."""

    def __init__(self, bitrate: int) -> None:
        err = ctypes.c_int(0)
        self._st = _lib.opus_encoder_create(SAMPLE_RATE, 1, _APPLICATION_AUDIO, ctypes.byref(err))
        _check(err.value)
        _check(_lib.opus_encoder_ctl(self._st, _SET_VBR, 1))
        _check(_lib.opus_encoder_ctl(self._st, _SET_COMPLEXITY, 10))
        _check(_lib.opus_encoder_ctl(self._st, _SET_SIGNAL, _SIGNAL_MUSIC))
        self.bitrate = 0
        self.set_bitrate(bitrate)
        self._out = ctypes.create_string_buffer(_MAX_PACKET)

    def set_bitrate(self, bitrate: int) -> None:
        _check(_lib.opus_encoder_ctl(self._st, _SET_BITRATE, int(bitrate)))
        self.bitrate = int(bitrate)

    def encode(self, pcm: bytes) -> bytes:
        """Encode exactly one frame (960 int16 samples, little endian)."""
        if len(pcm) != FRAME_SAMPLES * 2:
            raise ValueError(f"Frame muss {FRAME_SAMPLES * 2} Bytes haben, nicht {len(pcm)}")
        n = _check(_lib.opus_encode(self._st, pcm, FRAME_SAMPLES, self._out, _MAX_PACKET))
        return self._out.raw[:n]

    def __del__(self) -> None:
        st, self._st = getattr(self, "_st", None), None
        if st:
            _lib.opus_encoder_destroy(st)


class Decoder:
    """Mono 48 kHz Opus decoder (used by the end-to-end tests)."""

    def __init__(self) -> None:
        err = ctypes.c_int(0)
        self._st = _lib.opus_decoder_create(SAMPLE_RATE, 1, ctypes.byref(err))
        _check(err.value)
        self._max = SAMPLE_RATE * 120 // 1000
        self._out = ctypes.create_string_buffer(self._max * 2)

    def decode(self, packet: bytes) -> bytes:
        n = _check(_lib.opus_decode(self._st, packet, len(packet), self._out, self._max, 0))
        return self._out.raw[: n * 2]

    def __del__(self) -> None:
        st, self._st = getattr(self, "_st", None), None
        if st:
            _lib.opus_decoder_destroy(st)
