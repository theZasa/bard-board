"""Real-time mixer: sums active voices into 20 ms frames and hands them to the platform's audio sink."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .opus import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger("mixer")

FRAME_SECONDS = FRAME_MS / 1000


class MusicSource:
    """What the mixer needs from the music player."""

    active: bool
    volume: int

    def pull_frame(self) -> np.ndarray | None: ...


# Ducking: music gain while a sound plays, and how fast it moves per 20 ms frame.
DUCK_ATTACK_STEP = 0.2  # down in about 100 ms
DUCK_RELEASE_STEP = 0.03  # back up in about 600 ms


class AudioSink:
    """What the mixer needs from the platform (see platforms.base.AudioSink)."""

    ready: bool

    def send_frame(self, pcm: bytes) -> None: ...

    def end(self) -> None: ...

    async def drain(self) -> None: ...


@dataclass
class Voice:
    id: int
    sound_id: str
    name: str
    pcm: np.ndarray
    gain: float
    requested_by: str
    source: str
    started_at: float = field(default_factory=time.time)
    pos: int = 0

    @property
    def duration(self) -> float:
        return self.pcm.size / SAMPLE_RATE

    @property
    def elapsed(self) -> float:
        return self.pos / SAMPLE_RATE

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "sound_id": self.sound_id,
            "name": self.name,
            "requested_by": self.requested_by,
            "source": self.source,
            "duration": round(self.duration, 3),
            "elapsed": round(self.elapsed, 3),
            "started_at": self.started_at,
        }


class Mixer:
    def __init__(
        self,
        sink: AudioSink,
        *,
        max_voices: int,
        overlap: bool,
        volume: int,
        on_change: Callable[[], None] | None = None,
        music: MusicSource | None = None,
        duck_level: float = 0.25,
    ) -> None:
        self.sink = sink
        self.max_voices = max_voices
        self.overlap = overlap
        self.volume = volume
        self.on_change = on_change or (lambda: None)
        self.voices: list[Voice] = []
        self.music = music
        self.duck_level = duck_level
        self._duck = 1.0
        self._ids = itertools.count(1)
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ control

    def play(self, sound_id: str, name: str, pcm: np.ndarray, *, gain: float, requested_by: str, source: str) -> Voice:
        if not self.overlap:
            self.voices.clear()
        while len(self.voices) >= self.max_voices:
            self.voices.pop(0)  # drop the oldest voice
        voice = Voice(next(self._ids), sound_id, name, pcm, gain, requested_by, source)
        self.voices.append(voice)
        self._wake.set()
        self.on_change()
        return voice

    def stop(self, voice_id: int | None = None) -> int:
        before = len(self.voices)
        if voice_id is None:
            self.voices.clear()
        else:
            self.voices = [v for v in self.voices if v.id != voice_id]
        stopped = before - len(self.voices)
        if stopped:
            self.on_change()
        return stopped

    def stop_matching(self, predicate: Callable[[Voice], bool]) -> int:
        before = len(self.voices)
        self.voices = [v for v in self.voices if not predicate(v)]
        stopped = before - len(self.voices)
        if stopped:
            self.on_change()
        return stopped

    def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, int(volume)))

    @property
    def playing(self) -> bool:
        return bool(self.voices)

    def wake(self) -> None:
        """Start streaming (e.g. music became active or Mumble reconnected)."""
        self._wake.set()

    def _music_active(self) -> bool:
        return self.music is not None and self.music.active

    @staticmethod
    def _curve(percent: float) -> float:
        # Perceptual curve: 50 % is about -12 dB, 100 % is unity gain.
        v = max(0.0, percent) / 100
        return v * v

    # ---------------------------------------------------------------- mixing

    def mix_frame(self) -> np.ndarray:
        """Mix the next 20 ms and advance all voices. Finished voices are removed."""
        acc = np.zeros(FRAME_SAMPLES, dtype=np.float32)
        master = self._curve(self.volume)
        if self._music_active():
            target = self.duck_level if self.voices else 1.0
            if target < self._duck:
                self._duck = max(target, self._duck - DUCK_ATTACK_STEP)
            else:
                self._duck = min(target, self._duck + DUCK_RELEASE_STEP)
            chunk = self.music.pull_frame()
            if chunk is not None:
                gain = master * self._curve(self.music.volume) * self._duck
                acc[: chunk.size] += chunk[:FRAME_SAMPLES].astype(np.float32) * gain
        else:
            self._duck = self.duck_level if self.voices else 1.0
        finished = []
        for voice in self.voices:
            chunk = voice.pcm[voice.pos : voice.pos + FRAME_SAMPLES]
            acc[: chunk.size] += chunk.astype(np.float32) * (voice.gain * master)
            voice.pos += FRAME_SAMPLES
            if voice.pos >= voice.pcm.size:
                finished.append(voice)
        if finished:
            self.voices = [v for v in self.voices if v not in finished]
            self.on_change()
        peak = float(np.max(np.abs(acc))) if acc.size else 0.0
        if peak > 32767:
            # Soft knee limiter for overlapping sounds instead of hard clipping.
            acc = np.tanh(acc / 32768.0) * 32767.0
        return acc.astype(np.int16)

    async def run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._stream()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the mixer must survive anything
                log.exception("Fehler im Mixer - Wiedergabe gestoppt")
                self.stop()

    async def _stream(self) -> None:
        next_tick = time.perf_counter()
        sent = terminated = False
        while self.voices or self._music_active():
            if not self.sink.ready:
                self.stop()
                break
            frame = self.mix_frame()
            last = not self.voices and not self._music_active()
            self.sink.send_frame(frame.tobytes())
            sent = True
            if last:
                self.sink.end()
                terminated = True
            await self.sink.drain()
            next_tick += FRAME_SECONDS
            delay = next_tick - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.25:
                # We fell far behind (e.g. host suspended): resync instead of bursting.
                next_tick = time.perf_counter()
        if sent and not terminated and self.sink.ready:
            self.sink.end()  # stopped early
        await self.sink.drain()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="mixer")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
