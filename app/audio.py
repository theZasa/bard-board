"""Decoding of sound files to 48 kHz mono PCM, loudness normalization and caching."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections import OrderedDict
from pathlib import Path

import numpy as np

from .opus import SAMPLE_RATE

log = logging.getLogger("audio")

# Bump when the processing below changes so stale cache files are ignored.
PROCESSING_VERSION = 2
TARGET_RMS_DBFS = -18.0
PEAK_LIMIT_DBFS = -1.0
MAX_GAIN_DB = 20.0
GATE_DBFS = -50.0
SILENCE_DBFS = -55.0


class DecodeError(RuntimeError):
    pass


def _db_to_lin(db: float) -> float:
    return float(10 ** (db / 20))


def _lin_to_db(value: float) -> float:
    return float(20 * np.log10(max(value, 1e-9)))


def normalize(samples: np.ndarray) -> np.ndarray:
    """Bring a clip to a common loudness, measured as gated RMS, without exceeding the peak limit."""
    if samples.size == 0:
        return samples
    x = samples.astype(np.float32) / 32768.0
    block = SAMPLE_RATE // 20  # 50 ms blocks
    n = (x.size // block) * block
    if n:
        blocks = x[:n].reshape(-1, block)
        rms = np.sqrt(np.mean(blocks**2, axis=1))
        active = rms[rms > _db_to_lin(GATE_DBFS)]
        loudness = float(np.sqrt(np.mean(active**2))) if active.size else 0.0
    else:
        loudness = float(np.sqrt(np.mean(x**2)))
    if loudness <= 0:
        return samples
    peak = float(np.max(np.abs(x)))
    gain_db = TARGET_RMS_DBFS - _lin_to_db(loudness)
    gain_db = min(gain_db, MAX_GAIN_DB, PEAK_LIMIT_DBFS - _lin_to_db(peak))
    y = x * _db_to_lin(gain_db)
    return np.clip(np.round(y * 32767.0), -32768, 32767).astype(np.int16)


def trim_silence(samples: np.ndarray) -> np.ndarray:
    """Remove leading and trailing silence so sounds start instantly."""
    if samples.size == 0:
        return samples
    threshold = _db_to_lin(SILENCE_DBFS) * 32768
    loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > threshold)
    if loud.size == 0:
        return samples
    pad = SAMPLE_RATE // 100  # keep 10 ms around the content
    start = max(0, int(loud[0]) - pad)
    end = min(samples.size, int(loud[-1]) + pad)
    return samples[start:end]


async def decode_file(path: Path, max_seconds: int) -> np.ndarray:
    """Decode any format ffmpeg understands into mono int16 PCM at 48 kHz."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-t",
        str(max_seconds),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "s16le",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        message = err.decode(errors="replace").strip().splitlines()
        raise DecodeError(message[-1] if message else f"ffmpeg Exit-Code {proc.returncode}")
    if len(out) < 2:
        raise DecodeError("Datei enthält keine Audiodaten")
    return np.frombuffer(out[: len(out) // 2 * 2], dtype=np.int16).copy()


class AudioCache:
    """Processed PCM on disk plus a bounded in-memory LRU."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        normalize: bool,
        trim: bool,
        max_seconds: int,
        memory_budget: int = 256 * 1024 * 1024,
        disk: bool = True,
    ) -> None:
        self.cache_dir = cache_dir
        self.disk = disk  # False: decode on demand, keep only the in-memory LRU (for huge collections)
        self.normalize = normalize
        self.trim = trim
        self.max_seconds = max_seconds
        self.memory_budget = memory_budget
        self._memory: OrderedDict[str, np.ndarray] = OrderedDict()
        self._memory_bytes = 0
        self._locks: dict[str, asyncio.Lock] = {}
        self._decode_slots = asyncio.Semaphore(max(1, min(4, (os.cpu_count() or 2))))
        if disk:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def key_for(self, path: Path) -> str:
        st = path.stat()
        raw = f"{PROCESSING_VERSION}|{self.normalize}|{self.trim}|{self.max_seconds}|{path}|{st.st_size}|{st.st_mtime_ns}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def _remember(self, key: str, pcm: np.ndarray) -> None:
        if key in self._memory:
            self._memory.move_to_end(key)
            return
        self._memory[key] = pcm
        self._memory_bytes += pcm.nbytes
        while self._memory_bytes > self.memory_budget and len(self._memory) > 1:
            _, dropped = self._memory.popitem(last=False)
            self._memory_bytes -= dropped.nbytes

    def is_cached(self, path: Path) -> bool:
        try:
            key = self.key_for(path)
        except OSError:
            return False
        return key in self._memory or (self.disk and (self.cache_dir / f"{key}.pcm").exists())

    async def get(self, path: Path) -> np.ndarray:
        key = self.key_for(path)
        if key in self._memory:
            self._memory.move_to_end(key)
            return self._memory[key]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._memory:
                return self._memory[key]
            file = self.cache_dir / f"{key}.pcm"
            if self.disk and file.exists():
                pcm = np.fromfile(file, dtype=np.int16)
            else:
                async with self._decode_slots:
                    pcm = await decode_file(path, self.max_seconds)
                    pcm = await asyncio.to_thread(self._process, pcm)
                if self.disk:
                    tmp = file.with_suffix(".tmp")
                    pcm.tofile(tmp)
                    os.replace(tmp, file)
            self._remember(key, pcm)
        self._locks.pop(key, None)
        return pcm

    def _process(self, pcm: np.ndarray) -> np.ndarray:
        if self.trim:
            pcm = trim_silence(pcm)
        if self.normalize:
            pcm = normalize(pcm)
        return pcm

    def prune(self, valid_paths: list[Path]) -> int:
        """Delete cache files that no longer belong to any sound."""
        keep = set()
        for p in valid_paths:
            try:
                keep.add(f"{self.key_for(p)}.pcm")
            except OSError:
                continue
        removed = 0
        for f in self.cache_dir.glob("*.pcm"):
            if f.name not in keep:
                f.unlink(missing_ok=True)
                removed += 1
        return removed
