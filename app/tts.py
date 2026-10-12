"""Text-to-speech with Piper (offline neural voices) and synthetic signal sounds."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from pathlib import Path

import numpy as np

from .audio import normalize
from .opus import SAMPLE_RATE

log = logging.getLogger("tts")

CACHE_ENTRIES = 64


class TTSError(RuntimeError):
    pass


def clean_text(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + " ..."
    return text


async def resample(pcm: bytes, rate: int) -> np.ndarray:
    """High quality resampling to 48 kHz with ffmpeg (Piper voices use 16 or 22.05 kHz)."""
    if rate == SAMPLE_RATE:
        return np.frombuffer(pcm, dtype=np.int16).copy()
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1",
        "-i", "pipe:0", "-af", f"aresample={SAMPLE_RATE}:resampler=soxr",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(pcm)
    if proc.returncode != 0:
        # soxr is optional in some ffmpeg builds, fall back to the default resampler
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1",
            "-i", "pipe:0", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(pcm)
        if proc.returncode != 0:
            raise TTSError(f"Resampling fehlgeschlagen: {err.decode(errors='replace').strip()[-200:]}")
    return np.frombuffer(out[: len(out) // 2 * 2], dtype=np.int16).copy()


class Speech:
    """Lazy loaded Piper voice with a small cache (pull timer phrases repeat a lot)."""

    def __init__(self, model: Path, *, max_chars: int) -> None:
        self.model = model
        self.max_chars = max_chars
        self._voice = None
        self._load_error = ""
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()

    def _model_problem(self) -> str:
        """Why the voice model cannot be used ("" if it looks fine). Never raises."""
        try:
            with self.model.open("rb") as fh:
                fh.read(1)
            with Path(f"{self.model}.json").open("rb") as fh:
                fh.read(1)
        except FileNotFoundError as exc:
            return f"Kein Stimmenmodell gefunden ({exc.filename})"
        except OSError as exc:
            return f"Stimmenmodell nicht lesbar ({exc.filename}: {exc.strerror})"
        return ""

    @property
    def available(self) -> bool:
        return not self.unavailable_reason

    @property
    def unavailable_reason(self) -> str:
        if self._load_error:
            return self._load_error
        problem = self._model_problem()
        if problem:
            return problem
        try:
            import piper  # noqa: F401
        except ImportError:
            return "Piper ist nicht installiert"
        return ""

    def _load(self):
        if self._voice is None:
            from piper import PiperVoice

            log.info("Lade Stimme %s", self.model.name)
            self._voice = PiperVoice.load(str(self.model))
        return self._voice

    def _synth(self, text: str) -> tuple[bytes, int]:
        voice = self._load()
        chunks = list(voice.synthesize(text))
        if not chunks:
            raise TTSError("Kein Audio erzeugt")
        return b"".join(c.audio_int16_bytes for c in chunks), chunks[0].sample_rate

    async def speak(self, text: str) -> np.ndarray:
        text = clean_text(text, self.max_chars)
        if not text:
            raise TTSError("Kein Text")
        if text in self._cache:
            self._cache.move_to_end(text)
            return self._cache[text]
        if not self.available:
            raise TTSError(self.unavailable_reason)
        async with self._lock:  # one synthesis at a time, the voice is not thread safe
            try:
                raw, rate = await asyncio.to_thread(self._synth, text)
            except TTSError:
                raise
            except Exception as exc:  # noqa: BLE001 - model or onnxruntime problems
                log.exception("Sprachausgabe fehlgeschlagen")
                if self._voice is None:
                    self._load_error = f"Stimme konnte nicht geladen werden: {exc}"
                raise TTSError(f"Sprachausgabe fehlgeschlagen: {exc}") from exc
        pcm = normalize(await resample(raw, rate))
        self._cache[text] = pcm
        while len(self._cache) > CACHE_ENTRIES:
            self._cache.popitem(last=False)
        return pcm

    async def warm_up(self) -> None:
        """Load the voice in the background so the first /tts is fast."""
        problem = self.unavailable_reason
        if problem:
            log.warning("Sprachausgabe nicht verfügbar: %s", problem)
            return
        try:
            await asyncio.to_thread(self._load)
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"Stimme konnte nicht geladen werden: {exc}"
            log.error(self._load_error)


def _lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    alpha = 1 - np.exp(-2 * np.pi * cutoff / SAMPLE_RATE)
    y = np.empty_like(x)
    acc = 0.0
    for i, value in enumerate(x):  # computed once at startup, a plain loop is fine
        acc += alpha * (value - acc)
        y[i] = acc
    return y


def war_horn(seconds: float = 1.8) -> np.ndarray:
    """A synthetic brass horn (no samples needed): detuned sawtooth chord, pitch scoop, vibrato, filter."""
    n = int(SAMPLE_RATE * seconds)
    t = np.arange(n) / SAMPLE_RATE
    scoop = 1 - 0.06 * np.exp(-t / 0.08)  # starts slightly flat and slides up like a real horn
    vibrato = 1 + 0.004 * np.sin(2 * np.pi * 5.2 * t) * np.clip((t - 0.3) / 0.4, 0, 1)
    signal = np.zeros(n)
    for base, amp in ((98.0, 1.0), (98.6, 0.7), (147.0, 0.55), (196.0, 0.3)):
        phase = np.cumsum(base * scoop * vibrato / SAMPLE_RATE)
        signal += amp * (2 * (phase % 1.0) - 1)
    # darker, more brass than buzz: two passes of a one pole low pass
    signal = _lowpass(_lowpass(signal, 900.0), 900.0)
    envelope = np.minimum(1.0, t / 0.09) * np.minimum(1.0, (seconds - t) / 0.45)
    signal *= np.clip(envelope, 0, 1)
    pcm = (signal / np.max(np.abs(signal)) * 20000).astype(np.int16)
    return normalize(pcm)


def beep(freq: float = 880.0, seconds: float = 0.15) -> np.ndarray:
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    envelope = np.minimum(1.0, t / 0.005) * np.minimum(1.0, (seconds - t) / 0.03)
    return (np.sin(2 * np.pi * freq * t) * envelope * 16000).astype(np.int16)
