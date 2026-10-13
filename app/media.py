"""Online media via yt-dlp: resolving links, searches and playlists, stream URLs and clips."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("media")

_URL = re.compile(r"^https?://", re.IGNORECASE)
_TIME = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:[.,]\d+)?)$")
_ERROR_PREFIX = re.compile(r"^(?:ERROR:\s*)?(?:\[[^\]]+\]\s*)?(?:[\w-]+:\s+)?")
RESOLVE_TIMEOUT = 60.0
COOKIE_COPY_MAX_AGE = 6 * 3600  # copies handed to yt-dlp subprocesses, removed after this
BOT_CHECK_HINT = ("YouTube verlangt eine Anmeldung (Schutz vor Bots, typisch für Server im Rechenzentrum). "
                  "Lade im Webinterface im Regler-Dialog unter „YouTube-Cookies“ eine Cookie-Datei hoch.")
EXPIRED_COOKIES_HINT = ("YouTube lehnt die hinterlegten Cookies ab (abgelaufen oder abgemeldet). "
                        "Lade im Webinterface unter „YouTube-Cookies“ eine neue Cookie-Datei hoch.")
# Errors where another attempt (fresh stream URL, then yt-dlp's own downloader) can help.
# googlevideo answers 403 for some stream variants and IP or token mismatches.
_RETRYABLE = re.compile(
    r"\b(?:401|403|410|429|5\d\d)\b|forbidden|server returned|http error|connection|timed? ?out|"
    r"input/output error|end of file|network|unreachable|refused|reset by peer",
    re.IGNORECASE,
)
# Stream URLs of most sites expire; YouTube puts the expiry into the URL ("expire=").
DEFAULT_STREAM_TTL = 30 * 60


class MediaError(RuntimeError):
    """A link could not be resolved or played. The message is shown to users."""


def is_url(text: str) -> bool:
    return bool(_URL.match(text.strip()))


def parse_time(text: str) -> float:
    """Parse "83", "1:23", "1:02:03" or "1:23.5" into seconds."""
    m = _TIME.match(text.strip())
    if not m:
        raise ValueError(f"Ungültige Zeitangabe: {text!r} (Beispiel: 1:23)")
    parts = [p for p in m.groups() if p is not None]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part.replace(",", "."))
    return seconds


def is_retryable(message: str) -> bool:
    return bool(_RETRYABLE.search(message))


def format_time(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _clean_error(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    text = _ERROR_PREFIX.sub("", text, count=1).strip()
    lowered = text.casefold()
    if "private video" in lowered:
        return "Das Video ist privat"
    if "video unavailable" in lowered or "not available" in lowered:
        return "Das Video ist nicht verfügbar"
    if "sign in to confirm your age" in lowered or "age-restricted" in lowered:
        return "Das Video hat eine Altersbeschränkung"
    if "confirm you" in lowered and "not a bot" in lowered:
        return BOT_CHECK_HINT
    if "unsupported url" in lowered:
        return "Dieser Link wird nicht unterstützt"
    text = re.sub(r"\s*\(caused by .*$", "", text)
    if re.search(r"\b403\b", text) and "forbidden" in lowered:
        return f"Zugriff verweigert (HTTP 403): {text[:150]}"
    return text[:200]


COOKIES_MAX_BYTES = 1_000_000
COOKIES_HEADER = "# Netscape HTTP Cookie File"
# Present only when the export was made while signed in to YouTube; without them the file does not help.
_LOGIN_COOKIES = {"LOGIN_INFO", "SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "__Secure-3PAPISID"}


def validate_cookies(data: bytes) -> str:
    """Check an uploaded cookies.txt (Netscape format, e.g. from "Get cookies.txt LOCALLY").

    Returns the text to store; raises MediaError with a German reason. The content is never logged.
    """
    if len(data) > COOKIES_MAX_BYTES:
        raise MediaError("Die Datei ist zu groß für eine Cookie-Datei")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MediaError("Das ist keine Cookie-Datei (keine Textdatei)") from exc
    youtube = signed_in = False
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for line in lines:
        cookie = line[len("#HttpOnly_"):] if line.startswith("#HttpOnly_") else line
        if not cookie.strip() or cookie.startswith("#"):
            continue
        fields = cookie.split("\t")
        if len(fields) != 7:
            raise MediaError("Das ist keine Cookie-Datei im Netscape-Format (cookies.txt)")
        if fields[0].lstrip(".").endswith("youtube.com"):
            youtube = True
            signed_in = signed_in or fields[5] in _LOGIN_COOKIES
    if not youtube:
        raise MediaError("Die Datei enthält keine Cookies von youtube.com")
    if not signed_in:
        raise MediaError("In der Datei fehlen die Anmelde-Cookies: beim Export war niemand bei YouTube angemeldet")
    body = "\n".join(line for line in lines if not line.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")))
    return f"{COOKIES_HEADER}\n{body.strip()}\n"


def _wants_single_video(url: str) -> bool:
    """watch?v=X&list=Y means "this video", not the whole playlist."""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "list" not in query:
        return False
    return "v" in query or parsed.netloc.endswith("youtu.be")


@dataclass
class TrackInfo:
    source: str  # what to hand yt-dlp again when the track is played
    title: str
    webpage: str  # link for humans
    duration: float | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    is_live: bool = False
    starts_at: float | None = None  # set for announced livestreams and premieres that have not begun yet

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "TrackInfo":
        keys = ("source", "title", "webpage", "duration", "thumbnail", "uploader", "is_live", "starts_at")
        known = {k: data.get(k) for k in keys}
        return cls(**{k: v for k, v in known.items() if v is not None})


@dataclass
class Stream:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    duration: float | None = None
    is_live: bool = False
    expires_at: float = 0.0

    @property
    def fresh(self) -> bool:
        return time.time() < self.expires_at - 60


@dataclass
class Lookup:
    tracks: list[TrackInfo]
    playlist_title: str | None = None


class _Logger:
    """Routes yt-dlp output into our logging instead of stderr."""

    def debug(self, msg: str) -> None:
        log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)


def _thumbnail(entry: dict) -> str | None:
    if entry.get("ie_key") == "Youtube" or entry.get("extractor_key") == "Youtube":
        vid = entry.get("id")
        if vid:
            return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = entry.get("thumbnails") or []
    return thumbs[-1].get("url") if thumbs else None


def _upcoming(info: dict) -> float | None:
    """Start time of a livestream or premiere that has not begun yet (0 when unknown), else None."""
    if info.get("live_status") != "is_upcoming":
        return None
    return float(info.get("release_timestamp") or 0)


# Audio only where possible. Livestreams often have no audio-only format, then the smallest video
# variant that still carries good audio (YouTube's 360p live stream has 128 kbit/s AAC).
FORMAT = "bestaudio/best[height<=360][acodec!=none]/best[acodec!=none]/best"


def _expiry(url: str) -> float:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    try:
        return float(query["expire"][0])
    except (KeyError, ValueError, IndexError):
        return time.time() + DEFAULT_STREAM_TTL


class Resolver:
    """Thin async wrapper around yt-dlp (imported lazily, it may have been updated at startup)."""

    def __init__(self, cache_dir: Path, *, max_playlist: int = 100, cookies_file: Path | None = None) -> None:
        self.cache_dir = cache_dir
        self.max_playlist = max_playlist
        # YouTube cookies (Netscape format) for servers that YouTube asks to sign in; optional.
        self.cookies_file = cookies_file

    def _cookie_copy(self) -> Path | None:
        """A private copy of the cookie file for one yt-dlp run.

        yt-dlp writes the cookie jar back when it finishes; parallel runs (lookup, stream, download)
        would otherwise overwrite each other's version of the one file the person uploaded.
        """
        if self.cookies_file is None or not self.cookies_file.is_file():
            return None
        folder = self.cache_dir / "cookies"
        folder.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for old in folder.glob("*.txt"):
            try:
                if now - old.stat().st_mtime > COOKIE_COPY_MAX_AGE:
                    old.unlink()
            except OSError:
                pass
        copy = folder / f"{os.getpid()}-{time.monotonic_ns()}.txt"
        shutil.copyfile(self.cookies_file, copy)
        os.chmod(copy, 0o600)
        return copy

    def _error(self, exc: Exception) -> MediaError:
        message = _clean_error(exc)
        if message == BOT_CHECK_HINT and self.cookies_file is not None and self.cookies_file.is_file():
            message = EXPIRED_COOKIES_HINT
        return MediaError(message)

    def _extract(self, target: str, opts: dict) -> dict | None:
        import yt_dlp

        cookies = self._cookie_copy()
        if cookies is not None:
            opts = {**opts, "cookiefile": str(cookies)}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(target, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise self._error(exc) from exc
        finally:
            if cookies is not None:
                cookies.unlink(missing_ok=True)

    def _options(self, **extra) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _Logger(),
            "skip_download": True,
            "socket_timeout": 15,
            "extractor_retries": 2,
            "cachedir": str(self.cache_dir),
            "format": FORMAT,
        }
        opts.update(extra)
        return opts

    async def _run(self, func, *args):
        try:
            return await asyncio.wait_for(asyncio.to_thread(func, *args), RESOLVE_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise MediaError("Zeitüberschreitung beim Abrufen") from exc

    async def lookup(self, query: str) -> Lookup:
        query = query.strip()
        if not query:
            raise MediaError("Link oder Suchbegriff fehlt")
        return await self._run(self._lookup_sync, query)

    def _lookup_sync(self, query: str) -> Lookup:
        search = not is_url(query)
        target = f"ytsearch1:{query}" if search else query
        opts = self._options(
            extract_flat="in_playlist",
            noplaylist=not search and _wants_single_video(query),
            playlistend=self.max_playlist,
        )
        info = self._extract(target, opts)
        if not info:
            raise MediaError("Nichts gefunden")
        if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
            entries = [e for e in (info.get("entries") or []) if e][: self.max_playlist]
            tracks = [self._from_entry(e) for e in entries]
            if not tracks:
                raise MediaError(f"Keine Treffer für \"{query}\"" if search else "Die Playlist ist leer")
            return Lookup(tracks, None if search else info.get("title"))
        return Lookup([self._from_info(info)])

    @staticmethod
    def _from_entry(e: dict) -> TrackInfo:
        # Flat entries carry the watch URL in "url"; generic entries carry the media URL there.
        source = e.get("url") or e.get("webpage_url")
        webpage = e.get("url") if e.get("_type") == "url" else (e.get("webpage_url") or source)
        return TrackInfo(
            source=source,
            title=e.get("fulltitle") or e.get("title") or source,
            webpage=webpage or source,
            duration=e.get("duration"),
            thumbnail=_thumbnail(e),
            uploader=e.get("uploader") or e.get("channel"),
            is_live=bool(e.get("is_live") or e.get("live_status") == "is_live"),
            starts_at=_upcoming(e),
        )

    @staticmethod
    def _from_info(info: dict) -> TrackInfo:
        source = info.get("webpage_url") or info.get("original_url") or info.get("url")
        return TrackInfo(
            source=source,
            # yt-dlp appends date and time to the title of livestreams, "fulltitle" is the original one
            title=info.get("fulltitle") or info.get("title") or source,
            webpage=source,
            duration=info.get("duration"),
            thumbnail=_thumbnail(info),
            uploader=info.get("uploader") or info.get("channel"),
            is_live=bool(info.get("is_live") or info.get("live_status") == "is_live"),
            starts_at=_upcoming(info),
        )

    async def stream(self, source: str) -> Stream:
        return await self._run(self._stream_sync, source)

    def _stream_sync(self, source: str) -> Stream:
        info = self._extract(source, self._options(noplaylist=True))
        if info and info.get("entries"):
            info = next((e for e in info["entries"] if e), None)
        if not info:
            raise MediaError("Nichts gefunden")
        url = info.get("url")
        headers = dict(info.get("http_headers") or {})
        if not url and info.get("requested_formats"):
            fmt = info["requested_formats"][0]
            url, headers = fmt.get("url"), dict(fmt.get("http_headers") or headers)
        if not url:
            raise MediaError("Kein abspielbares Audio gefunden")
        return Stream(
            url=url,
            headers=headers,
            duration=info.get("duration"),
            is_live=bool(info.get("is_live") or info.get("live_status") == "is_live"),
            expires_at=_expiry(url),
        )

    def download_command(self, source: str) -> list[str]:
        """yt-dlp writing the audio to stdout with its own downloader.

        It knows YouTube's peculiarities (chunked range requests, tokens, retries) that a plain
        ffmpeg HTTP request does not, so it is the fallback when googlevideo answers 403.
        """
        cookies = self._cookie_copy()
        return [
            sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-progress", "--no-playlist",
            "--no-part", "--format", FORMAT, "--output", "-", "--socket-timeout", "15",
            "--retries", "3", "--fragment-retries", "3", "--cache-dir", str(self.cache_dir),
            *(["--cookies", str(cookies)] if cookies is not None else []), "--", source,
        ]

    @staticmethod
    def version() -> str:
        try:
            from yt_dlp.version import __version__
        except ImportError:
            return "nicht installiert"
        return __version__


def ffmpeg_input_args(stream: Stream, start: float = 0.0) -> list[str]:
    args: list[str] = ["-nostdin", "-hide_banner", "-loglevel", "error"]
    if stream.url.startswith(("http://", "https://")):
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
                 "-reconnect_delay_max", "5", "-rw_timeout", "20000000"]
        if stream.headers:
            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in stream.headers.items())]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", stream.url, "-vn"]
    return args


async def probe_duration(stream: Stream) -> float | None:
    """Ask ffprobe for the length when the site did not report one (direct media links)."""
    args = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1"]
    if stream.url.startswith(("http://", "https://")) and stream.headers:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in stream.headers.items())]
    proc = await asyncio.create_subprocess_exec(
        *args, stream.url, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 15)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        return None
    try:
        value = float(out.decode().strip())
    except ValueError:
        return None
    return value if value > 0 else None


def pipe_input_args() -> list[str]:
    """ffmpeg input options when the audio arrives on stdin from the yt-dlp downloader."""
    return ["-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vn"]


def last_error_line(stderr: bytes) -> str:
    lines = [line.strip() for line in stderr.decode(errors="replace").splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("ERROR")] or lines
    return _clean_error(Exception(errors[-1])) if errors else ""


async def spawn_pipeline(feeder: list[str], ffmpeg_args: list[str], *, stdout) -> tuple[
        asyncio.subprocess.Process, asyncio.subprocess.Process]:
    """Start `feeder | ffmpeg`. The feeder's stderr is piped so its error can be reported."""
    read_fd, write_fd = os.pipe()
    try:
        feeder_proc = await asyncio.create_subprocess_exec(
            *feeder, stdin=asyncio.subprocess.DEVNULL, stdout=write_fd, stderr=asyncio.subprocess.PIPE
        )
        try:
            ffmpeg_proc = await asyncio.create_subprocess_exec(
                "ffmpeg", *ffmpeg_args, stdin=read_fd, stdout=stdout, stderr=asyncio.subprocess.PIPE
            )
        except BaseException:
            feeder_proc.kill()
            raise
    finally:
        os.close(read_fd)
        os.close(write_fd)
    return feeder_proc, ffmpeg_proc


async def make_clip(resolver: Resolver, source: str, start: float, end: float, target: Path, tmp_dir: Path) -> Path:
    """Cut [start, end] out of an online video and store it as MP3.

    Tries ffmpeg on the stream URL (fast, seeks with range requests), then a fresh URL, then
    yt-dlp's own downloader piped into ffmpeg. The file is written to tmp_dir first, so the
    library scanner never sees a half written sound.
    """
    if end <= start:
        raise MediaError("Das Ende muss nach dem Start liegen")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"clip-{time.time_ns()}.mp3"
    output = ["-t", f"{end - start:.3f}", "-ac", "2", "-ar", "48000", "-c:a", "libmp3lame", "-q:a", "2", "-y", str(tmp)]
    error = ""
    try:
        for attempt in ("direct", "direct-fresh", "downloader"):
            procs: list[asyncio.subprocess.Process] = []
            if attempt == "downloader":
                procs = list(await spawn_pipeline(resolver.download_command(source),
                                                  [*pipe_input_args(), "-ss", f"{start:.3f}", *output],
                                                  stdout=asyncio.subprocess.DEVNULL))
            else:
                stream = await resolver.stream(source)
                if stream.duration and start >= stream.duration:
                    raise MediaError(f"Der Start liegt hinter dem Ende des Videos ({format_time(stream.duration)})")
                procs = [await asyncio.create_subprocess_exec(
                    "ffmpeg", *ffmpeg_input_args(stream, start), *output,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)]
            try:
                results = await asyncio.wait_for(asyncio.gather(*(p.communicate() for p in procs)), 300)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                for p in procs:
                    if p.returncode is None:
                        p.kill()
                raise MediaError("Zeitüberschreitung beim Schneiden") from exc
            ffmpeg_proc, ffmpeg_err = procs[-1], results[-1][1]
            if ffmpeg_proc.returncode == 0 and tmp.exists() and tmp.stat().st_size >= 1000:
                if attempt != "direct":
                    log.info("Clip über %s erstellt",
                             "yt-dlp-Downloader" if attempt == "downloader" else "frische Stream-Adresse")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(tmp), target)
                return target
            feeder_err = last_error_line(results[0][1]) if len(procs) == 2 and procs[0].returncode else ""
            error = feeder_err or last_error_line(ffmpeg_err) or f"Exit-Code {ffmpeg_proc.returncode}"
            tmp.unlink(missing_ok=True)
            if not is_retryable(error):
                break
            log.warning("Clip-Versuch %s fehlgeschlagen: %s", attempt, error)
        raise MediaError(f"Schneiden fehlgeschlagen: {error}")
    finally:
        tmp.unlink(missing_ok=True)
