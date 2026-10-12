"""Music queue ("radio"): online audio is streamed through ffmpeg into the mixer."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import numpy as np

from .media import (
    MediaError,
    Resolver,
    Stream,
    TrackInfo,
    ffmpeg_input_args,
    format_time,
    is_retryable,
    last_error_line,
    pipe_input_args,
    probe_duration,
    spawn_pipeline,
)
from .opus import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger("music")

FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_SECONDS = FRAME_MS / 1000
BUFFER_FRAMES = 150  # 3 s of decoded audio between ffmpeg and the mixer
PREBUFFER_FRAMES = 15  # start playing once 300 ms are decoded
PREBUFFER_TIMEOUT = 25.0
MAX_RESTARTS = 3
HEALTHY_SECONDS = 60.0  # a stream that played this long since the last hiccup gets a fresh restart budget
T = TypeVar("T")


class _Skipped(Exception):
    pass


@dataclass
class Track:
    id: int
    info: TrackInfo
    requested_by: str
    source: str  # "mumble" | "web"
    added_at: float = field(default_factory=time.time)
    start_at: float = 0.0  # resume position after a restart of the bot
    stream: Stream | None = None

    def to_json(self) -> dict:
        data = self.info.to_json()
        data.update(id=self.id, requested_by=self.requested_by, origin=self.source, added_at=self.added_at)
        return data

    def label(self) -> str:
        if self.info.is_live:
            return f"{self.info.title} (LIVE)"
        return f"{self.info.title} ({format_time(self.info.duration)})" if self.info.duration else self.info.title


@dataclass
class AddResult:
    tracks: list[Track]
    playlist_title: str | None
    skipped: int
    position: int  # 1-based queue position of the first added track (0 = plays right now)


class MusicPlayer:
    def __init__(
        self,
        resolver: Resolver,
        *,
        state_file: Path,
        volume: int,
        max_seconds: int,
        normalize: bool,
        on_change: Callable[[], None],
        on_notice: Callable[[str], None],
        on_active: Callable[[], None],
        on_track: Callable[[Track], None] | None = None,
    ) -> None:
        self.resolver = resolver
        self.state_file = state_file
        self.volume = volume
        self.max_seconds = max_seconds
        self.normalize = normalize
        self.on_change = on_change
        self.on_notice = on_notice
        self.on_active = on_active
        self.on_track = on_track or (lambda track: None)

        self.queue: list[Track] = []
        self.current: Track | None = None
        self.paused = False
        self.loading = False
        self.position = 0.0
        self.underruns = 0

        self._ids = itertools.count(1)
        self._changed = asyncio.Event()
        self._skip = asyncio.Event()
        self._reopen = asyncio.Event()  # pause/resume of a livestream: drop the connection, rejoin live
        self._finished = asyncio.Event()
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue(BUFFER_FRAMES)
        self._eof = False
        self._decoder: asyncio.subprocess.Process | None = None
        self._feeder: asyncio.subprocess.Process | None = None  # yt-dlp downloader in fallback mode
        self._decoder_rc: int | None = None
        self._decoder_err = ""
        self._reader: asyncio.Task | None = None
        self._probe: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        self._load_state()

    # ------------------------------------------------------------------ state

    @property
    def active(self) -> bool:
        """True while the mixer should pull frames."""
        return self.current is not None and not self.paused and not self.loading

    @property
    def state(self) -> str:
        if self.current is not None and self.loading:
            return "loading"
        if self.paused and (self.current is not None or self.queue):
            return "paused"
        if self.current is not None:
            return "playing"
        return "idle"

    def to_json(self) -> dict:
        return {
            "state": self.state,
            "current": self.current.to_json() if self.current else None,
            "position": round(self.position, 2),
            "queue": [t.to_json() for t in self.queue],
            "volume": self.volume,
            "max_minutes": self.max_seconds // 60,
            "ts": time.time(),
        }

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_file.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("music.json nicht lesbar (%s) - starte mit leerer Warteschlange", exc)
            return
        self.volume = int(data.get("volume", self.volume))
        tracks = []
        for item in ([data["current"]] if data.get("current") else []) + list(data.get("queue", [])):
            try:
                track = Track(next(self._ids), TrackInfo.from_json(item), item.get("requested_by", "?"),
                              item.get("origin", "web"))
            except (TypeError, KeyError):
                continue
            tracks.append(track)
        if tracks and data.get("current") and not tracks[0].info.is_live:  # a livestream rejoins live
            tracks[0].start_at = float(data.get("position", 0.0))
        self.queue = tracks
        # Do not blast music right after a restart, wait for /resume or the play button.
        self.paused = bool(tracks)

    def _save_state(self) -> None:
        data = {
            "volume": self.volume,
            "current": self.current.to_json() if self.current else None,
            "position": round(self.position, 2),
            "queue": [t.to_json() for t in self.queue],
        }
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
            os.replace(tmp, self.state_file)
        except OSError:
            log.exception("music.json konnte nicht gespeichert werden")

    def _changed_now(self) -> None:
        self._save_state()
        self._changed.set()
        self.on_change()

    # ---------------------------------------------------------------- control

    async def add(self, query: str, *, requested_by: str, source: str, play_next: bool = False) -> AddResult:
        lookup = await self.resolver.lookup(query)
        tracks: list[Track] = []
        skipped = 0
        reasons: list[str] = []
        for info in lookup.tracks:
            if info.starts_at is not None:
                skipped += 1
                when = time.strftime(" am %d.%m. um %H:%M Uhr", time.localtime(info.starts_at)) if info.starts_at else ""
                reasons.append(f"\"{info.title}\" startet erst{when}")
                continue
            if not info.is_live and info.duration and info.duration > self.max_seconds:
                skipped += 1
                reasons.append(
                    f"\"{info.title}\" ist zu lang ({format_time(info.duration)}, "
                    f"maximal {format_time(self.max_seconds)})"
                )
                continue
            tracks.append(Track(next(self._ids), info, requested_by, source))
        if not tracks:
            raise MediaError(reasons[0] if reasons else "Nichts zum Abspielen gefunden")
        position = 1 if play_next else len(self.queue) + 1
        if play_next:
            self.queue[0:0] = tracks
        else:
            self.queue.extend(tracks)
        if self.current is None and self.paused:
            self.paused = False  # somebody wants music: start (again)
        if self.current is None:
            position -= 1  # the first queued title starts right away (0 = now)
        self._changed_now()
        self.on_active()
        return AddResult(tracks, lookup.playlist_title, skipped, position)

    def skip(self) -> Track | None:
        track = self.current
        if track is not None:
            self._skip.set()
        return track

    def pause(self) -> bool:
        if self.paused or (self.current is None and not self.queue):
            return False
        self.paused = True
        if self.current is not None and self.current.info.is_live:
            self._reopen.set()  # no point in buffering a livestream nobody hears, rejoin live later
        self._changed_now()
        return True

    def resume(self) -> bool:
        if not self.paused:
            return False
        self.paused = False
        self._changed_now()
        self.on_active()
        return True

    def clear(self) -> int:
        count = len(self.queue)
        self.queue.clear()
        self._changed_now()
        return count

    def shuffle(self) -> None:
        random.shuffle(self.queue)
        self._changed_now()

    def remove(self, track_id: int) -> Track | None:
        for i, track in enumerate(self.queue):
            if track.id == track_id:
                del self.queue[i]
                self._changed_now()
                return track
        return None

    def remove_at(self, position: int) -> Track | None:
        """Remove by 1-based queue position (as shown by /queue)."""
        if 1 <= position <= len(self.queue):
            return self.remove(self.queue[position - 1].id)
        return None

    def move(self, track_id: int, index: int) -> bool:
        for i, track in enumerate(self.queue):
            if track.id == track_id:
                del self.queue[i]
                self.queue.insert(max(0, min(index, len(self.queue))), track)
                self._changed_now()
                return True
        return False

    def set_volume(self, volume: int) -> int:
        self.volume = max(0, min(100, int(volume)))
        self._save_state()
        self.on_change()
        return self.volume

    # ------------------------------------------------------------- mixer side

    def pull_frame(self) -> np.ndarray | None:
        """Next 20 ms of music for the mixer, None on underrun or at the end of a track."""
        try:
            chunk = self._frames.get_nowait()
        except asyncio.QueueEmpty:
            if self._eof:
                self._finished.set()
            else:
                self.underruns += 1
            return None
        if chunk is None:
            self._finished.set()
            return None
        self.position += FRAME_SECONDS
        return np.frombuffer(chunk, dtype=np.int16)

    # ---------------------------------------------------------------- playback

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="music")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._stop_decoder()
        self._save_state()

    async def _run(self) -> None:
        while True:
            if self.current is None and (self.paused or not self.queue):
                self._changed.clear()
                await self._changed.wait()
                continue
            if self.current is None:
                self.current = self.queue.pop(0)
                self.position = self.current.start_at
                self.current.start_at = 0.0
                self._changed_now()
            try:
                await self._play(self.current)
            except asyncio.CancelledError:
                raise  # shutdown: keep current and position for music.json
            except Exception:  # noqa: BLE001 - one broken track must not stop the radio
                log.exception("Unerwarteter Fehler bei %s", self.current.info.title if self.current else "?")
            await self._stop_decoder()
            self.loading = False
            self.current = None
            self.position = 0.0
            self._changed_now()

    async def _until_skip(self, awaitable: Awaitable[T]) -> T:
        task = asyncio.ensure_future(awaitable)
        skip = asyncio.create_task(self._skip.wait())
        try:
            done, _ = await asyncio.wait({task, skip}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            skip.cancel()
        if task in done:
            return task.result()
        task.cancel()
        raise _Skipped()

    async def _play(self, track: Track) -> None:
        self._skip.clear()
        self._reopen.clear()
        restarts = 0
        healthy_from = self.position
        announced = False
        mode = "direct"
        try:
            while True:
                if self.paused:  # paused livestream: wait without holding a connection
                    await self._until_skip(self._until_resumed())
                mode = await self._until_skip(self._open(track, mode))
                self.loading = False
                self.on_change()
                self.on_active()
                if not announced:
                    self.on_track(track)
                    announced = True
                if await self._until_skip(self._until_end()) == "reopen":
                    self._reopen.clear()
                    await self._stop_decoder()
                    continue
                if self._ended_normally(track):
                    if track.info.is_live:
                        self.on_notice(f"Der Livestream \"{track.info.title}\" ist zu Ende.")
                    return
                if self.position - healthy_from >= HEALTHY_SECONDS:
                    restarts = 0  # long streams may hiccup now and then, only rapid failures count
                restarts += 1
                healthy_from = self.position
                detail = self._decoder_err or f"ffmpeg Exit-Code {self._decoder_rc}"
                if restarts > MAX_RESTARTS:
                    raise MediaError(f"Stream bricht immer wieder ab ({detail})")
                where = "live" if track.info.is_live else f"bei {format_time(self.position)}"
                log.warning("Stream von %s abgebrochen %s (%s) - setze fort", track.info.title, where, detail)
                track.stream = None  # the URL may have expired, resolve again
                await self._stop_decoder()
        except _Skipped:
            return
        except MediaError as exc:
            log.warning("Kann %s nicht abspielen: %s", track.info.title, exc)
            self.on_notice(f"\"{track.info.title}\" kann nicht abgespielt werden: {exc}")

    async def _until_end(self) -> str:
        """Wait until the decoded stream is played out ("finished") or a livestream must reconnect."""
        finished = asyncio.create_task(self._finished.wait())
        reopen = asyncio.create_task(self._reopen.wait())
        try:
            done, _ = await asyncio.wait({finished, reopen}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            finished.cancel()
            reopen.cancel()
        return "reopen" if reopen in done else "finished"

    async def _until_resumed(self) -> None:
        while self.paused:
            self._changed.clear()
            await self._changed.wait()

    async def _open(self, track: Track, mode: str) -> str:
        """Open the track at self.position and prebuffer. Returns the mode that worked.

        Ladder for errors like YouTube's 403: stream URL with ffmpeg, a freshly resolved URL,
        then yt-dlp's own downloader piped into ffmpeg (kept for later restarts of this track).
        """
        self.loading = True
        self.on_change()
        attempts = ["downloader"] if mode == "downloader" else ["direct", "direct-fresh", "downloader"]
        error: MediaError | None = None
        for attempt in attempts:
            if attempt == "downloader":
                await self._start_decoder(None, self.position, feeder=self.resolver.download_command(track.info.source))
            else:
                if attempt == "direct-fresh" or track.stream is None or not track.stream.fresh:
                    track.stream = await self.resolver.stream(track.info.source)
                self._check_stream(track, track.stream)
                await self._start_decoder(track.stream, self.position)
            try:
                await self._prebuffer()
            except MediaError as exc:
                error = exc
                await self._stop_decoder()
                if not is_retryable(str(exc)):
                    raise
                log.warning("%s: Versuch \"%s\" fehlgeschlagen (%s)", track.info.title, attempt, exc)
                continue
            if attempt != "direct":
                log.info("%s läuft über %s", track.info.title,
                         "yt-dlp-Downloader" if attempt == "downloader" else "frische Stream-Adresse")
            if track.info.duration is None and not track.info.is_live and track.stream is not None \
                    and attempt != "downloader" and self._probe is None:
                self._probe = asyncio.create_task(self._probe_duration(track, track.stream))
            return "downloader" if attempt == "downloader" else "direct"
        assert error is not None
        raise error

    def _check_stream(self, track: Track, stream: Stream) -> None:
        if stream.is_live:
            if not track.info.is_live:  # found out only now (e.g. a search result or playlist entry)
                track.info.is_live = True
                track.info.duration = None
                self.on_change()
            return
        if stream.duration and stream.duration > self.max_seconds:
            raise MediaError(f"zu lang ({format_time(stream.duration)}, maximal {format_time(self.max_seconds)})")
        if track.info.duration is None and stream.duration:
            track.info.duration = stream.duration

    async def _probe_duration(self, track: Track, stream: Stream) -> None:
        try:
            duration = await probe_duration(stream)
        finally:
            self._probe = None
        if duration and track is self.current and track.info.duration is None:
            track.info.duration = duration
            self.on_change()

    def _ended_normally(self, track: Track) -> bool:
        if self._decoder_rc not in (0, None):
            return False
        if track.info.is_live:
            return True  # ffmpeg only ends cleanly when the broadcaster ended the stream
        expected = min(track.info.duration or 0, self.max_seconds)
        # A stream that silently ends early (server closed the connection) is resumed.
        return not expected or self.position >= expected - 1.5

    async def _prebuffer(self) -> None:
        deadline = time.monotonic() + PREBUFFER_TIMEOUT
        while self._frames.qsize() < PREBUFFER_FRAMES and not self._eof:
            if time.monotonic() > deadline:
                raise MediaError("Der Stream liefert keine Daten")
            await asyncio.sleep(0.02)
        if self._eof and self._frames.qsize() <= 1 and self._decoder_rc:
            raise MediaError(self._decoder_err or "Der Stream konnte nicht geöffnet werden")

    async def _start_decoder(self, stream: Stream | None, start: float, *, feeder: list[str] | None = None) -> None:
        await self._stop_decoder()
        live = self.current is not None and self.current.info.is_live
        if live:
            start = 0.0  # a livestream always (re)joins at the live edge and has no length limit
        output = [] if live else ["-t", f"{max(1.0, self.max_seconds - start):.3f}"]
        if self.normalize:
            output += ["-af", "loudnorm=I=-18:TP=-1.5:LRA=11"]
        output += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
        if feeder is not None:
            # Piped input cannot seek: ffmpeg decodes and drops everything before `start`.
            seek = ["-ss", f"{start:.3f}"] if start > 0 else []
            self._feeder, self._decoder = await spawn_pipeline(
                feeder, [*pipe_input_args(), *seek, *output], stdout=asyncio.subprocess.PIPE
            )
        else:
            assert stream is not None
            self._decoder = await asyncio.create_subprocess_exec(
                "ffmpeg", *ffmpeg_input_args(stream, start), *output,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        self._reader = asyncio.create_task(self._read(self._decoder, self._feeder), name="music-decoder")

    async def _read(self, proc: asyncio.subprocess.Process, feeder: asyncio.subprocess.Process | None) -> None:
        assert proc.stdout is not None and proc.stderr is not None
        err_task = asyncio.create_task(proc.stderr.read())
        feeder_err = asyncio.create_task(feeder.stderr.read()) if feeder and feeder.stderr else None
        try:
            while True:
                try:
                    chunk = await proc.stdout.readexactly(FRAME_BYTES)
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        await self._frames.put(exc.partial.ljust(FRAME_BYTES, b"\0"))
                    break
                await self._frames.put(chunk)
            self._decoder_rc = await proc.wait()
            self._decoder_err = last_error_line(await err_task)[:200]
            if feeder is not None and feeder_err is not None:
                feeder_rc = await feeder.wait()
                if feeder_rc != 0:
                    # The downloader's error explains more than ffmpeg's "invalid input".
                    self._decoder_rc = feeder_rc
                    self._decoder_err = (last_error_line(await feeder_err) or self._decoder_err)[:200]
        finally:
            err_task.cancel()
            if feeder_err is not None:
                feeder_err.cancel()
            self._eof = True
            try:
                self._frames.put_nowait(None)
            except asyncio.QueueFull:
                pass  # pull_frame() reports the end once the buffer is drained and _eof is set

    async def _stop_decoder(self) -> None:
        proc, self._decoder = self._decoder, None
        feeder, self._feeder = self._feeder, None
        reader, self._reader = self._reader, None
        for p in (feeder, proc):
            if p is not None and p.returncode is None:
                p.kill()
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for p in (feeder, proc):
            if p is not None:
                try:
                    await asyncio.wait_for(p.wait(), 5)
                except (TimeoutError, asyncio.TimeoutError):
                    pass
        self._frames = asyncio.Queue(BUFFER_FRAMES)
        self._eof = False
        self._decoder_rc = None
        self._decoder_err = ""
        self._finished = asyncio.Event()
