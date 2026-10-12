"""End-to-end test against a real Mumble server.

Starts the soundboard as a subprocess, connects a "human" test client and checks what a real user
would experience: chat replies, audible audio (frequency analysis), following, web API and reconnects.

Usage (server must be running, SuperUser password required to create channels):
    python tools/e2e_test.py --host 127.0.0.1 --superuser-password secret --sounds /tmp/sb/sounds
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server as http_server
import json
import os
import shutil
import signal
import threading
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.commands import strip_html  # noqa: E402
from app.mumble import Mumble_pb2 as pb  # noqa: E402
from app.mumble.client import MumbleClient, TextMessage, User  # noqa: E402
from app.opus import SAMPLE_RATE, Decoder  # noqa: E402

BOT_NAME = "SoundboardTest"


class Recorder:
    def __init__(self) -> None:
        self.texts: list[TextMessage] = []
        self.audio: dict[int, list[bytes]] = {}
        self.terminators: dict[int, int] = {}
        self.times: dict[int, list[tuple[float, bool]]] = {}
        self.decoders: dict[int, Decoder] = {}
        self.ready = asyncio.Event()

    async def on_ready(self) -> None:
        self.ready.set()

    async def on_disconnected(self, reason: str) -> None:
        self.ready.clear()

    async def on_text_message(self, msg: TextMessage) -> None:
        self.texts.append(msg)

    async def on_user_changed(self, user: User, old: User | None) -> None:
        pass

    async def on_user_removed(self, user: User) -> None:
        pass

    async def on_permission_denied(self, text: str) -> None:
        print("  permission denied:", text)

    def on_voice(self, packet) -> None:
        dec = self.decoders.setdefault(packet.session, Decoder())
        self.audio.setdefault(packet.session, []).append(dec.decode(packet.opus))
        self.times.setdefault(packet.session, []).append((time.monotonic(), packet.terminator))
        if packet.terminator:
            self.terminators[packet.session] = self.terminators.get(packet.session, 0) + 1

    def reset(self) -> None:
        self.texts.clear()
        self.audio.clear()
        self.terminators.clear()
        self.times.clear()

    def bursts(self, session: int) -> list[float]:
        """Start times of each transmission (a new one starts after a terminator)."""
        starts, new = [], True
        for ts, terminator in self.times.get(session, []):
            if new:
                starts.append(ts)
            new = terminator
        return starts

    def pcm(self, session: int) -> np.ndarray:
        return np.frombuffer(b"".join(self.audio.get(session, [])), dtype=np.int16)


def band(pcm: np.ndarray, freq: float, width: float = 15) -> float:
    """Spectral magnitude around freq (relative measure for comparisons)."""
    if pcm.size < 2048:
        return 0.0
    x = pcm.astype(np.float32)
    spectrum = np.abs(np.fft.rfft(x * np.hanning(x.size))) / x.size
    freqs = np.fft.rfftfreq(x.size, 1 / SAMPLE_RATE)
    sel = (freqs > freq - width) & (freqs < freq + width)
    return float(spectrum[sel].max()) if sel.any() else 0.0


def make_media(directory: Path) -> None:
    """Test "online" media: tones with known frequencies, a playlist page and a broken file."""
    directory.mkdir(parents=True, exist_ok=True)

    def tone(name: str, freq: int, seconds: float, extra: list[str]) -> None:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                        *extra, str(directory / name)], check=True)

    tone("tone300.mp3", 300, 14, ["-ar", "44100", "-ac", "2", "-b:a", "128k"])
    tone("tone500.webm", 500, 10, ["-c:a", "libopus", "-b:a", "96k"])
    tone("tone700.m4a", 700, 10, ["-c:a", "aac", "-b:a", "96k"])
    (directory / "list.html").write_text(
        "<html><head><title>Testplaylist</title></head><body>"
        '<audio src="tone500.webm"></audio><audio src="tone700.m4a"></audio></body></html>'
    )
    (directory / "kaputt.mp3").write_bytes(b"das ist kein audio" * 100)


class RangeHandler(http_server.SimpleHTTPRequestHandler):
    """Static files with HTTP Range support, like real media CDNs (needed to seek in MP4/M4A).

    Special prefixes simulate YouTube's googlevideo servers rejecting some stream requests:
    /noffmpeg/<file>   403 for every direct ffmpeg request (only yt-dlp's own downloader gets through)
    /once403/<file>    403 for the first direct ffmpeg request, later ones work (fresh URL helps)
    /always403/<file>  403 for everybody
    ffmpeg is recognised by the "Icy-MetaData" header it sends by default.
    """

    seen_once: set[str] = set()

    def log_message(self, *args, **kwargs) -> None:
        pass

    def send_head(self):
        self._remaining = None  # handler objects are reused for keep-alive requests
        for prefix in ("/noffmpeg/", "/once403/", "/always403/"):
            if self.path.startswith(prefix):
                from_ffmpeg = "Icy-MetaData" in self.headers
                name = self.path[len(prefix):]
                forbidden = prefix == "/always403/" or (prefix == "/noffmpeg/" and from_ffmpeg)
                if prefix == "/once403/" and from_ffmpeg and name not in self.seen_once:
                    self.seen_once.add(name)
                    forbidden = True
                if forbidden:
                    self.send_error(403, "Forbidden")
                    return None
                self.path = "/" + name
                break
        path = Path(self.translate_path(self.path))
        rng = self.headers.get("Range")
        if not rng or not path.is_file():
            return super().send_head()
        size = path.stat().st_size
        start_s, _, end_s = rng.removeprefix("bytes=").partition("-")
        start = int(start_s or 0)
        end = min(int(end_s) if end_s else size - 1, size - 1)
        if start >= size:
            self.send_error(416)
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile) -> None:
        try:
            self._copy(source, outputfile)
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffmpeg hangs up mid-file on skip, stop and seek: expected, not an error

    def _copy(self, source, outputfile) -> None:
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(65536, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)


COLLECTION_INDEX = """\ufeffCharakter;Datei;Text (Deutsch);Text (Englisch);Situation;Stimmung;Wwise-ID
Bente Jorgensen;Wem sagst du das.mp3;Wem sagst du das.;Tell me about it.;AT_Bente_1;Positve;1
Bente Jorgensen;AI_DRYBENTE_X_00001.mp3;Mein Schiff ist schneller.;My ship is faster.;AT_Bente_2;Negative;2
Sir Archibald Blake;Kennste nicht.mp3;Kennste nicht? Kein Witz.;;AT_Blake_1;;3
_Ohne Zuordnung;123456.mp3;;;;;4
"""


def make_collection(root: Path) -> None:
    """A small game voice line export like the real Anno 1800 one: speaker folders, index CSV, code names."""
    coll = root / "Anno 1800"
    for rel, freq in (("Bente Jorgensen/Wem sagst du das.mp3", 520), ("Bente Jorgensen/AI_DRYBENTE_X_00001.mp3", 620),
                      ("Sir Archibald Blake/Kennste nicht.mp3", 760), ("_Ohne Zuordnung/123456.mp3", 900)):
        (coll / rel).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1.2",
                        str(coll / rel)], check=True)
    (coll / "_index.csv").write_text(COLLECTION_INDEX, "utf-8")


def make_dota(root: Path) -> None:
    """A second collection without index (text from file names), a second take and a file still being written."""
    coll = root / "Dota 2"
    for rel, freq in (("Axe/A Keen worthy to fight.mp3", 480), ("Axe/A Keen worthy to fight (2).mp3", 480),
                      ("Pudge/Fresh meat.mp3", 680)):
        (coll / rel).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1.2",
                        str(coll / rel)], check=True)
    (coll / "Pudge" / "Get over here.mp3.part").write_bytes(b"halb")


def serve(directory: Path, port: int) -> http_server.ThreadingHTTPServer:
    handler = functools.partial(RangeHandler, directory=str(directory))
    server = http_server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def dominant_frequency(pcm: np.ndarray) -> float:
    if pcm.size < 2048:
        return 0.0
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float32) * np.hanning(pcm.size)))
    freqs = np.fft.rfftfreq(pcm.size, 1 / SAMPLE_RATE)
    return float(freqs[int(np.argmax(spectrum))])


class Failure(AssertionError):
    pass


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{' - ' + detail if detail else ''}")


async def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


def http(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


async def connect(host: str, port: int, name: str, password: str = "") -> tuple[MumbleClient, Recorder, asyncio.Task]:
    rec = Recorder()
    client = MumbleClient(host=host, port=port, username=name, password=password, listener=rec, client_type=0)
    task = asyncio.create_task(client.run_forever())
    await asyncio.wait_for(rec.ready.wait(), 10)
    return client, rec, task


async def ensure_channels(host: str, port: int, password: str) -> None:
    admin, _, task = await connect(host, port, "SuperUser", password)
    for path in ("Gaming", "Gaming/Dota", "AFK"):
        if admin.find_channel(path):
            continue
        parent_path, _, name = path.rpartition("/")
        parent = admin.find_channel(parent_path) if parent_path else admin.channels[0]
        admin._send(pb.ChannelState(parent=parent.id, name=name, temporary=False))
        await wait_for(lambda p=path: admin.find_channel(p) is not None)
    await admin.close()
    task.cancel()


async def run(args: argparse.Namespace) -> int:
    await ensure_channels(args.host, args.port, args.superuser_password)
    data_dir = Path(tempfile.mkdtemp(prefix="sb-e2e-"))
    sounds = data_dir / "sounds"
    shutil.copytree(args.sounds, sounds)  # the clip test writes into the sounds folder
    # A line copied onto the board by an older version: "<Sammlung>/<Sprecher>/...", played 15 times and
    # used as a join sound. The bot moves it once into "<Sammlung>" and keeps all of that.
    old = sounds / "Anno 1800" / "Bente Jorgensen" / "Alte Kachel.mp3"
    old.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=560:duration=1",
                    str(old)], check=True)
    (data_dir / "library.json").write_text(json.dumps({"Anno 1800/Bente Jorgensen/Alte Kachel.mp3": {
        "plays": 15, "favorite": True, "volume": 120}}), "utf-8")
    (data_dir / "joinsounds.json").write_text(json.dumps({"noah": {
        "user": "Noah", "sound": "Anno 1800/Bente Jorgensen/Alte Kachel.mp3", "set_by": "Zasa", "ts": 0}}), "utf-8")
    make_media(data_dir / "media")
    # A livestream like YouTube serves it: HLS written in real time, no end
    (data_dir / "media" / "live").mkdir()
    live_gen = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "sine=frequency=610", "-c:a", "aac", "-f", "hls",
         "-hls_time", "1", "-hls_list_size", "6", "-hls_flags", "delete_segments",
         str(data_dir / "media" / "live" / "index.m3u8")], stdin=subprocess.DEVNULL)
    make_collection(data_dir / "extern")  # the original folder the export keeps filling
    make_dota(data_dir / "extern")
    stale = data_dir / "collections" / "Anno 1800" / "Bente Jorgensen"  # an older, smaller copy in the project
    stale.mkdir(parents=True)
    shutil.copy(data_dir / "extern" / "Anno 1800" / "Bente Jorgensen" / "Wem sagst du das.mp3", stale)
    args.extern_dir = data_dir / "extern" / "Anno 1800"
    media_server = serve(data_dir / "media", args.media_port)
    args.media = f"http://127.0.0.1:{args.media_port}"
    args.sounds_dir = sounds
    args.data_dir = data_dir
    web = f"http://127.0.0.1:{args.web_port}"
    env = dict(
        os.environ,
        MUMBLE_HOST=args.host,
        MUMBLE_PORT=str(args.port),
        MUMBLE_USER=BOT_NAME,
        MUMBLE_CHANNEL="AFK",
        MUMBLE_FOLLOW_USER="Zasa",
        SOUNDS_DIR=str(sounds),
        COLLECTIONS_DIRS=f"{data_dir / 'extern'},{data_dir / 'collections'}",
        YTDLP_AUTO_UPDATE="false",
        MUSIC_DEFAULT_VOLUME="100",
        DATA_DIR=str(data_dir),
        WEB_HOST="127.0.0.1",
        WEB_PORT=str(args.web_port),
        COMMAND_COOLDOWN="0",
        # Allocate via mmap so native pointers live above 4 GiB like in the Docker image.
        MALLOC_MMAP_THRESHOLD_="0",
        DEFAULT_VOLUME="100",
        LIVE_IDLE_MINUTES="0.1",
        # No internet for the bot, even where the machine has it (CI runners): every test medium is
        # served over plain HTTP from localhost, and the YouTube search must fail predictably.
        HTTPS_PROXY="http://127.0.0.1:9",
        https_proxy="http://127.0.0.1:9",
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
    )
    log_file = open(data_dir / "bot.log", "w")
    bot = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT)
    args.bot_pid = bot.pid
    try:
        return await scenario(args, web, bot)
    finally:
        bot.terminate()
        try:
            bot.wait(10)
        except subprocess.TimeoutExpired:
            bot.kill()
        log_file.close()
        media_server.shutdown()
        live_gen.kill()
        print(f"\nBot-Log: {data_dir / 'bot.log'}")


async def scenario(args: argparse.Namespace, web: str, bot: subprocess.Popen) -> int:
    print("Bot startet ...")
    ok = await wait_for(lambda: _state(web).get("status", {}).get("connected", False), 20, 0.3)
    check("Bot verbindet sich", ok)
    if not ok:
        return 1
    in_afk = await wait_for(lambda: _state(web)["status"]["channel"] == "AFK", 5, 0.2)
    check("Ohne Follow-User online: Bot geht in MUMBLE_CHANNEL", in_afk, str(_state(web)["status"]["channel"]))
    state = _state(web)
    old = next((s for s in state["library"]["sounds"] if s["name"] == "Alte Kachel"), {})
    check("Alte Sammlungs-Kachel liegt jetzt flach in „Anno 1800“ mit Sprecher",
          old.get("category") == "Anno 1800" and old.get("speaker") == "Bente Jorgensen", str(old)[:160])
    check("Dabei bleiben Zähler, Favorit und Lautstärke erhalten",
          (old.get("plays"), old.get("favorite"), old.get("volume")) == (15, True, 120), str(old)[:160])
    noah_js = next((j for j in state["extras"]["joinsounds"] if j["user"] == "Noah"), {})
    check("… und der Join-Sound zeigt auf die verschobene Datei",
          noah_js.get("sound") == "Alte Kachel" and not noah_js.get("missing"), str(noah_js))
    check("Leere alte Unterordner sind weg", not (args.sounds_dir / "Anno 1800" / "Bente Jorgensen").exists())

    zasa, rec, _ = await connect(args.host, args.port, "Zasa")
    gaming = zasa.find_channel("Gaming")
    dota = zasa.find_channel("Gaming/Dota")
    zasa.move_to(gaming.id)
    bot_user = lambda: zasa.find_user(BOT_NAME)  # noqa: E731
    check("Bot folgt Zasa nach Gaming", await wait_for(lambda: bot_user() and bot_user().channel_id == gaming.id))
    zasa.move_to(dota.id)
    check("Bot folgt Zasa nach Gaming/Dota", await wait_for(lambda: bot_user() and bot_user().channel_id == dota.id))
    bot_session = bot_user().session

    def say(text: str, private: bool = False) -> None:
        if private:
            zasa.send_text(text, session=bot_session)
        else:
            zasa.send_text(text, channel_id=zasa.myself.channel_id)

    async def reply(contains: str, timeout: float = 3.0) -> str:
        found = await wait_for(lambda: any(contains in strip_html(t.message) for t in rec.texts), timeout)
        texts = [strip_html(t.message) for t in rec.texts]
        return next((t for t in texts if contains in t), "") if found else " | ".join(texts)[:300]

    async def audio_test(name: str, command: str, freq: float, min_s: float, max_s: float) -> None:
        rec.reset()
        say(command)
        got_end = await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, max_s + 4)
        pcm = rec.pcm(bot_session)
        seconds = pcm.size / SAMPLE_RATE
        dom = dominant_frequency(pcm)
        rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2))) if pcm.size else 0.0
        check(
            name,
            got_end and min_s <= seconds <= max_s and abs(dom - freq) < 15 and rms > 1000,
            f"{seconds:.2f}s, {dom:.0f} Hz, RMS {rms:.0f}, Terminator {got_end}",
        )

    only = args.music_only or args.extras_only or args.collections_only
    if not only:
        rec.reset()
        say("/help")
        text = await reply("Soundboard-Befehle")
        check("/help antwortet im Kanal", "Soundboard-Befehle" in text and "/sb" in text, text[:80])

        await audio_test("/sb beep spielt 440 Hz hoerbar ab", "/sb beep", 440, 1.8, 2.3)
        await audio_test("Kurzform + Umlaut-Faltung: /alter kase", "/alter kase", 880, 1.3, 1.8)
        await audio_test("Kategorie/Name + Stille-Trim: !sb memes/leeroy", "!sb memes/leeroy", 660, 0.9, 1.15)

        rec.reset()
        say("<p>/sb&nbsp;beep</p>")  # what some clients send (HTML)
        check("HTML-formatierte Nachricht wird verstanden", await wait_for(lambda: bot_session in rec.audio, 3))
        await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 4)

        await asyncio.sleep(3)  # let the chat rate limit recover so /stop is sent without delay
        rec.reset()
        say("/sb beep")
        await wait_for(lambda: len(rec.audio.get(bot_session, [])) > 10, 3)
        t0 = time.monotonic()
        say("/stop")
        stopped = await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 3)
        frames = len(rec.audio.get(bot_session, []))
        check("/stop beendet sofort mit Terminator", stopped and frames < 60, f"{frames} Frames, {time.monotonic() - t0:.2f}s")

        rec.reset()
        say("/list")
        text = await reply("Sounds")
        check("/list listet Sounds mit Kategorie", "beep" in text and "Memes" in text and "alter käse" in text, text[:120])

        rec.reset()
        say("/xyzunbekannt")
        text = await reply("Kein Sound")
        check("Unbekannter Sound -> Hinweis", "Kein Sound" in text, text[:80])

        rec.reset()
        say("/vol 40")
        text = await reply("40 %")
        check("/vol 40 setzt Lautstaerke", "40 %" in text and _state(web)["volume"] == 40, text)

        rec.reset()
        say("/np", private=True)
        text = await reply("läuft nichts")
        check("Privatnachricht /np wird privat beantwortet", bool(text) and rec.texts and rec.texts[0].is_private, text)

        # Overlap: two sounds at the same time are mixed (triggered via web, chat is rate limited)
        http("POST", f"{web}/api/volume", {"volume": 100})
        ids = {s["name"]: s["id"] for s in _state(web)["library"]["sounds"]}
        rec.reset()
        http("POST", f"{web}/api/sounds/{ids['beep']}/play")
        await asyncio.sleep(0.3)
        http("POST", f"{web}/api/sounds/{ids['alter käse']}/play")
        await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 5)
        pcm = rec.pcm(bot_session)
        seg = pcm[int(0.6 * SAMPLE_RATE) : int(1.4 * SAMPLE_RATE)].astype(np.float32)
        spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size))) if seg.size else np.zeros(1)
        freqs = np.fft.rfftfreq(seg.size, 1 / SAMPLE_RATE) if seg.size else np.zeros(1)
        peak_at = lambda f: float(spec[(freqs > f - 10) & (freqs < f + 10)].max()) if seg.size else 0.0  # noqa: E731
        check("Ueberlagerung: 440 Hz und 880 Hz gleichzeitig hoerbar",
              peak_at(440) > 0.2 * peak_at(880) and peak_at(880) > 0.2 * peak_at(440),
              f"440: {peak_at(440):.0f}, 880: {peak_at(880):.0f}")

        # Web API
        state = _state(web)
        beep = next(s for s in state["library"]["sounds"] if s["name"] == "beep")
        rec.reset()
        status, _ = http("POST", f"{web}/api/sounds/{beep['id']}/play")
        played = await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 4)
        check("Web: Play-Button spielt im Kanal", status == 200 and played, f"HTTP {status}")
        check("Web: Play-Zaehler erhoeht", _state(web)["library"]["sounds"][0]["plays"] >= 1 or any(
            s["plays"] >= 3 for s in _state(web)["library"]["sounds"]))
        act = _state(web)["activity"]
        check("Aktivitaetslog enthaelt Mumble- und Web-Eintraege",
              any(a["source"] == "mumble" and a["user"] == "Zasa" for a in act) and any(a["source"] == "web" for a in act))

        # Unfollow and come
        rec.reset()
        say("/unfollow")
        await reply("niemandem")
        zasa.move_to(gaming.id)
        await asyncio.sleep(1)
        check("Nach /unfollow bleibt der Bot", bot_user().channel_id == dota.id)
        say("/come", private=True)  # bot is in another channel, so it has to be a private message
        check("/come holt den Bot", await wait_for(lambda: bot_user().channel_id == gaming.id))
        status, _ = http("POST", f"{web}/api/join", {"channel_id": zasa.find_channel("AFK").id})
        check("Web: Kanal wechseln", status == 200 and await wait_for(lambda: bot_user().channel_id == zasa.find_channel("AFK").id))
        http("POST", f"{web}/api/follow", {"user": "Zasa"})
        check("Web: Folgen aktivieren", await wait_for(lambda: bot_user().channel_id == gaming.id))

    if not only or args.music_only:
        await music_scenario(args, web, zasa, rec, bot_session, say, reply)
        await live_scenario(args, web, zasa, rec, bot_session, say, reply)
    if not only or args.extras_only:
        await extras_scenario(args, web, zasa, rec, bot_session, say, reply)
    if not only or args.collections_only:
        await collections_scenario(args, web, zasa, rec, bot_session, say, reply)

    # Reconnect after server restart
    if args.restart_cmd:
        print("Server-Neustart ...")
        subprocess.run(args.restart_cmd, shell=True, check=True)
        reconnected = await wait_for(lambda: _state(web).get("status", {}).get("connected", False), 40, 0.5)
        check("Bot verbindet sich nach Server-Neustart neu", reconnected)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} Checks bestanden")
    return 1 if failed else 0


async def music_scenario(args, web, zasa, rec, bot_session, say, reply) -> None:
    m = args.media
    print("Musik ...")
    await asyncio.sleep(3)  # let the chat rate limit recover
    last = lambda seconds: rec.pcm(bot_session)[-int(seconds * SAMPLE_RATE):]  # noqa: E731
    frames = lambda: len(rec.audio.get(bot_session, []))  # noqa: E731
    music = lambda: _state(web).get("music", {})  # noqa: E731

    # 1. Link via /play (as a Mumble client sends it: an HTML link)
    rec.reset()
    say(f'/play <a href="{m}/tone300.mp3">{m}/tone300.mp3</a>')
    text = await reply("tone300", 20)
    check("/play mit Link antwortet mit Titel", "tone300" in text, text[:100])
    playing = await wait_for(lambda: frames() > 100, 20)
    dom = dominant_frequency(last(1.0))
    check("Musik kommt hoerbar im Kanal an (300 Hz)", playing and abs(dom - 300) < 15, f"{dom:.0f} Hz, {frames()} Frames")
    st = music()
    check("Status: playing mit aktuellem Titel", st.get("state") == "playing" and "tone300" in (st.get("current") or {}).get("title", ""),
          str(st.get("state")))
    check("Ansage \"Jetzt: ...\" im Chat", await wait_for(lambda: any("Jetzt" in strip_html(t.message) for t in rec.texts), 3))

    # 2. Ducking while a sound plays
    await asyncio.sleep(1.0)
    pre = last(1.0)
    ids = {s["name"]: s["id"] for s in _state(web)["library"]["sounds"]}
    http("POST", f"{web}/api/sounds/{ids['beep']}/play")
    await asyncio.sleep(1.3)
    during = last(0.8)
    await asyncio.sleep(2.6)
    after = last(0.8)
    ratio = band(during, 300) / max(band(pre, 300), 1e-9)
    recovered = band(after, 300) / max(band(pre, 300), 1e-9)
    check("Ducking: Musik waehrend eines Sounds deutlich leiser", ratio < 0.45 and band(during, 440) > band(during, 300),
          f"Musik auf {ratio:.0%}, Beep {band(during, 440):.0f} vs Musik {band(during, 300):.0f}")
    check("Ducking: Musik danach wieder normal laut", recovered > 0.8, f"{recovered:.0%}")

    # 3. Playlist (plain text URL without HTML link)
    rec.reset()
    say(f"/play {m}/list.html")
    text = await reply("Playlist", 20)
    check("Playlist-Link fuegt alle Titel hinzu", "2 Titel" in text, text[:100])
    rec.reset()
    say("/queue")
    text = await reply("Warteschlange", 10)
    check("/queue zeigt aktuellen Titel und Warteschlange", "tone300" in text and "1. " in text and "2. " in text, text[:160])

    # 4. Skip
    rec.reset()
    say("/skip")
    text = await reply("übersprungen", 10)
    await wait_for(lambda: music().get("state") == "playing" and "(1)" in (music().get("current") or {}).get("title", ""), 15)
    await asyncio.sleep(1.5)
    dom = dominant_frequency(last(1.0))
    check("/skip springt zum naechsten Titel (500 Hz)", "übersprungen" in text and abs(dom - 500) < 15, f"{dom:.0f} Hz")

    # 5. Pause / resume
    rec.reset()
    say("/pause")
    paused = await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 5)
    count = frames()
    await asyncio.sleep(1.5)
    pos_paused = music().get("position", 0)
    check("/pause stoppt den Ton (Terminator, keine weiteren Frames)", paused and frames() == count and music().get("state") == "paused",
          f"{frames() - count} Frames nach Pause")
    say("/resume")
    resumed = await wait_for(lambda: frames() > count + 50, 6)
    await asyncio.sleep(0.5)
    check("/resume setzt an derselben Stelle fort", resumed and abs(dominant_frequency(last(0.5)) - 500) < 15
          and music().get("position", 0) >= pos_paused, f"Position {pos_paused:.1f} -> {music().get('position', 0):.1f}")

    # 6. Broken media: error message, the radio continues with the next title
    rec.reset()
    status, body = http("POST", f"{web}/api/music/add", {"query": f"{m}/kaputt.mp3", "next": True})
    check("Web: Titel als Naechstes einreihen", status == 200 and body.get("position") == 1, f"HTTP {status} {body}")
    say("/skip")
    notice = await reply("kann nicht abgespielt werden", 25)
    check("Kaputter Titel: Hinweis im Chat", "kann nicht abgespielt werden" in notice, notice[:120])
    moved_on = await wait_for(lambda: "(2)" in (music().get("current") or {}).get("title", "") and music().get("state") == "playing", 15)
    await asyncio.sleep(1.5)
    dom = dominant_frequency(last(1.0))
    check("Danach laeuft der naechste Titel (700 Hz)", moved_on and abs(dom - 700) < 15, f"{dom:.0f} Hz")

    # 7. Queue management via web and chat
    for name in ("tone300.mp3", "tone500.webm"):
        http("POST", f"{web}/api/music/add", {"query": f"{m}/{name}"})
    queue = music().get("queue", [])
    check("Web: zwei Titel in der Warteschlange", [q["title"] for q in queue] == ["tone300", "tone500"], str([q["title"] for q in queue]))
    if len(queue) == 2:
        http("POST", f"{web}/api/music/move", {"id": queue[1]["id"], "index": 0})
        order = [q["title"] for q in music().get("queue", [])]
        check("Web: Umsortieren", order == ["tone500", "tone300"], str(order))
        http("DELETE", f"{web}/api/music/queue/{queue[1]['id']}")
        check("Web: Titel entfernen", [q["title"] for q in music().get("queue", [])] == ["tone300"])
    rec.reset()
    say("/remove 1")
    text = await reply("entfernt", 10)
    check("/remove 1 entfernt per Chat", "tone300" in text and not music().get("queue"), text[:80])
    http("POST", f"{web}/api/music/add", {"query": f"{m}/tone300.mp3"})
    rec.reset()
    say("/clear")
    text = await reply("geleert", 10)
    check("/clear leert die Warteschlange", "1 Titel" in text and not music().get("queue"), text[:80])

    # 8. Music volume and /np
    rec.reset()
    say("/mvol 30")
    text = await reply("30 %", 10)
    check("/mvol setzt die Musik-Lautstaerke", music().get("volume") == 30, text[:60])
    say("/np")
    text = await reply("Läuft:", 10)
    check("/np zeigt den Musiktitel", "(2)" in text, text[:100])
    http("POST", f"{web}/api/music/volume", {"volume": 100})

    # 9. /stop pauses music too
    rec.reset()
    say("/stop")
    check("/stop pausiert auch die Musik", await wait_for(lambda: music().get("state") == "paused", 10), str(music().get("state")))

    # 10. Clip -> new sound
    rec.reset()
    say(f"/clip {m}/tone300.mp3 0:02 0:04 testclip")
    text = await reply("Gespeichert", 30)
    clip = args.sounds_dir / "YouTube Clips" / "testclip.mp3"
    check("/clip speichert einen neuen Sound", "testclip" in text and clip.exists(), text[:100])
    if "testclip" not in text and getattr(args, "bot_pid", None) and os.environ.get("E2E_DUMP"):
        os.kill(args.bot_pid, signal.SIGUSR1)
    await wait_for(lambda: any(s["name"] == "testclip" for s in _state(web)["library"]["sounds"]), 10)
    await asyncio.sleep(1)
    rec.reset()
    say("/sb testclip")
    ended = await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 8)
    pcm = rec.pcm(bot_session)
    seconds = pcm.size / SAMPLE_RATE
    check("Clip ist abspielbar (300 Hz, ca. 2 s)", ended and abs(dominant_frequency(pcm) - 300) < 15 and 1.6 < seconds < 2.4,
          f"{seconds:.2f}s, {dominant_frequency(pcm):.0f} Hz")
    status, body = http("POST", f"{web}/api/clip", {"url": f"{m}/tone300.mp3", "start": "0:05", "end": "0:03"})
    check("Web: ungueltiger Clip-Bereich wird abgelehnt", status == 422 and "Ende" in body.get("error", ""), f"HTTP {status}")
    status, body = http("POST", f"{web}/api/clip", {"url": f"{m}/tone300.mp3", "start": "0", "end": "5:00"})
    check("Web: zu langer Clip wird abgelehnt", status == 422, f"HTTP {status} {body.get('error', '')}")

    # 11. YouTube-like 403 errors (simulated CDN): retry with a fresh URL, then yt-dlp's own downloader
    http("POST", f"{web}/api/music/clear")
    http("POST", f"{web}/api/music/skip")
    await wait_for(lambda: music().get("current") is None, 10)
    for prefix, label in (("once403", "403 beim ersten Versuch: frische Adresse hilft"),
                          ("noffmpeg", "403 fuer ffmpeg: yt-dlp-Downloader springt ein")):
        rec.reset()
        status, _ = http("POST", f"{web}/api/music/add", {"query": f"{m}/{prefix}/tone300.mp3"})
        http("POST", f"{web}/api/music/resume")
        playing = await wait_for(lambda: music().get("state") == "playing" and frames() > 75, 30)
        await asyncio.sleep(1.0)
        dom = dominant_frequency(last(1.0))
        errors = [strip_html(t.message) for t in rec.texts if "nicht abgespielt" in strip_html(t.message)]
        check(label, status == 200 and playing and abs(dom - 300) < 15 and not errors,
              f"{dom:.0f} Hz, Fehler: {errors[:1]}")
        http("POST", f"{web}/api/music/skip")
        await wait_for(lambda: music().get("current") is None, 10)
    rec.reset()
    status, body = http("POST", f"{web}/api/music/add", {"query": f"{m}/always403/tone300.mp3"})
    check("403 fuer alle: verstaendliche Fehlermeldung statt Haenger", status == 422 and "403" in body.get("error", ""),
          f"HTTP {status} {body.get('error', '')[:80]}")
    rec.reset()
    say(f"/clip {m}/noffmpeg/tone500.webm 0:01 0:03 clip403")
    text = await reply("Gespeichert", 40)
    check("/clip trotz 403 fuer ffmpeg", "clip403" in text and (args.sounds_dir / "YouTube Clips" / "clip403.mp3").exists(),
          text[:100])

    # 12. Persistence and search without network
    http("POST", f"{web}/api/music/add", {"query": f"{m}/tone700.m4a"})
    await wait_for(lambda: music().get("state") == "playing", 15)
    saved = json.loads((args.data_dir / "music.json").read_text())
    check("Warteschlange wird gespeichert (music.json)", saved.get("current") is not None and saved.get("volume") == 100)
    rec.reset()
    say("/play never gonna give you up")
    text = await reply("Geht nicht", 40)
    check("Suche ohne Netz: verstaendliche Fehlermeldung", "Geht nicht" in text, text[:120])


async def extras_scenario(args, web, zasa, rec, bot_session, say, reply) -> None:
    print("Extras ...")
    http("POST", f"{web}/api/music/clear")
    http("POST", f"{web}/api/music/skip")
    await wait_for(lambda: _state(web)["music"].get("current") is None, 10)
    await asyncio.sleep(3)
    extras = lambda: _state(web).get("extras", {})  # noqa: E731
    frames = lambda: len(rec.audio.get(bot_session, []))  # noqa: E731
    check("TTS ist verfügbar", extras().get("tts", {}).get("available") is True, str(extras().get("tts")))

    # 1. Text-to-speech
    rec.reset()
    say("/tts Hallo zusammen, das ist ein Test der Sprachausgabe")
    spoke = await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 15)
    pcm = rec.pcm(bot_session)
    seconds = pcm.size / SAMPLE_RATE
    rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2))) if pcm.size else 0.0
    speechy = band(pcm, 300, 250) > 0 and dominant_frequency(pcm) < 1500
    check("/tts spricht hörbar im Kanal", spoke and 1.5 < seconds < 8 and rms > 800 and speechy,
          f"{seconds:.1f}s, RMS {rms:.0f}")
    rec.reset()
    status, _ = http("POST", f"{web}/api/tts", {"text": "Web Durchsage"})
    check("Web: Durchsage", status == 200 and await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 10),
          f"HTTP {status}")

    # 2. Pull timer: intro, 5..1, "Pull!" + horn exactly 10 s after the start
    await asyncio.sleep(2)
    rec.reset()
    say("/pull 10")
    text = await reply("Pull in 10", 8)
    check("/pull kündigt im Chat an", "Pull in 10" in text, text[:60])
    await wait_for(lambda: len(rec.bursts(bot_session)) >= 7 and rec.terminators.get(bot_session, 0) >= 7, 20)
    starts = rec.bursts(bot_session)
    span = starts[-1] - starts[0] if len(starts) >= 2 else 0
    gaps = [round(b - a, 2) for a, b in zip(starts[1:], starts[2:], strict=False)]
    check("Pull-Timer: Ansage, 5 Zahlen, Pull mit Horn", len(starts) == 7, f"{len(starts)} Durchsagen, Abstände {gaps}")
    check("Pull-Timer: Horn genau nach 10 s", abs(span - 10) < 0.4 and all(abs(g - 1) < 0.25 for g in gaps[:-1]),
          f"{span:.2f} s")
    last = np.frombuffer(b"".join(rec.audio[bot_session][-60:]), dtype=np.int16)
    check("Pull-Timer: Horn hörbar (tiefes Blech um 100 Hz)", band(last, 98, 8) > 0.3 * band(last, 440, 50),
          f"{band(last, 98, 8):.0f} vs {band(last, 440, 50):.0f}")
    check("Pull-Timer ist danach beendet", await wait_for(lambda: extras().get("pull") is None, 3))

    rec.reset()
    say("/pull 20")
    await wait_for(lambda: extras().get("pull") is not None, 8)
    await asyncio.sleep(2)
    say("/pull stop")
    text = await reply("abgebrochen", 8)
    count = frames()
    await asyncio.sleep(4)
    check("/pull stop bricht ab (nur noch die Abbruch-Ansage)", "abgebrochen" in text and extras().get("pull") is None
          and frames() - count < 200, f"{frames() - count} Frames danach")

    # 3. Join sounds
    rec.reset()
    say("/joinsound beep")
    text = await reply("Join-Sound", 10)
    check("/joinsound setzt den eigenen Join-Sound", "beep" in text, text[:80])
    say("/joinsound @Noah alter kase")
    text = await reply("Noahs", 10)
    check("/joinsound @Noah setzt fremden Join-Sound (ohne ADMINS darf das jeder)", "alter käse" in text, text[:80])
    noah, noah_rec, noah_task = await connect(args.host, args.port, "Noah")
    rec.reset()
    noah.move_to(zasa.myself.channel_id)
    played = await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 6)
    dom = dominant_frequency(rec.pcm(bot_session))
    check("Join-Sound beim Betreten des Kanals (880 Hz)", played and abs(dom - 880) < 15, f"{dom:.0f} Hz")
    lobby = zasa.find_channel("AFK")
    noah.move_to(lobby.id)
    await asyncio.sleep(1.5)
    rec.reset()
    noah.move_to(zasa.myself.channel_id)
    await asyncio.sleep(3)
    check("Cooldown: kein zweiter Join-Sound kurz danach", bot_session not in rec.audio)
    # Overwriting: the new sound must be stored AND play on the next join, cooldown or not
    # (otherwise nobody can try out a new join sound for ten minutes).
    rec.reset()
    say("/joinsound @Noah beep")
    text = await reply("Noahs", 10)
    entry = next((j for j in extras().get("joinsounds", []) if j["user"] == "Noah"), {})
    check("/joinsound überschreibt bestehenden Join-Sound", "beep" in text and entry.get("sound") == "beep",
          f"{text[:60]} | {entry}")
    noah.move_to(lobby.id)
    await asyncio.sleep(1.5)
    rec.reset()
    noah.move_to(zasa.myself.channel_id)
    played = await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 6)
    dom = dominant_frequency(rec.pcm(bot_session))
    check("Überschriebener Join-Sound spielt beim nächsten Betreten (440 Hz)", played and abs(dom - 440) < 15,
          f"{dom:.0f} Hz" if played else "nichts gespielt")
    say("/joinsound @Noah alter kase")
    await reply("alter käse", 10)
    await noah.close()
    noah_task.cancel()
    status, body = http("PUT", f"{web}/api/joinsounds", {"user": "Pia", "sound_id": next(
        s["id"] for s in _state(web)["library"]["sounds"] if s["name"] == "beep")})
    users = [j["user"] for j in body.get("joinsounds", [])]
    check("Web: Join-Sound setzen", status == 200 and users == ["Noah", "Pia", "Zasa"], str(users))
    status, body = http("PUT", f"{web}/api/joinsounds", {"user": "pia", "sound_id": next(
        s["id"] for s in _state(web)["library"]["sounds"] if s["name"] == "alter käse")})
    pia = [j for j in body.get("joinsounds", []) if j["user"].casefold() == "pia"]
    check("Web: Join-Sound überschreiben (auch mit anderer Schreibweise)",
          status == 200 and len(pia) == 1 and pia[0]["sound"] == "alter käse" and pia[0]["user"] == "Pia", str(pia))
    status, body = http("DELETE", f"{web}/api/joinsounds/Pia")
    check("Web: Join-Sound löschen", status == 200 and "Pia" not in [j["user"] for j in body.get("joinsounds", [])])
    sid = next(s["id"] for s in _state(web)["library"]["sounds"] if s["name"] == "alter käse")
    http("PATCH", f"{web}/api/sounds/{sid}", {"name": "alter gouda"})
    noah_entry = next((j for j in extras().get("joinsounds", []) if j["user"] == "Noah"), {})
    check("Umbenennen hält Join-Sound aktuell", noah_entry.get("sound") == "alter gouda", str(noah_entry))

    # 4. Aliases
    await asyncio.sleep(3)
    rec.reset()
    say("/alias lr memes/leeroy")
    text = await reply("Kürzel", 10)
    check("/alias legt Kürzel für Sound an", "/lr" in text and "leeroy" in text, text[:80])
    rec.reset()
    say("/lr")
    ended = await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 5)
    check("Kürzel spielt den Sound (660 Hz)", ended and abs(dominant_frequency(rec.pcm(bot_session)) - 660) < 15)
    rec.reset()
    say("/alias play beep")
    text = await reply("Befehl", 10)
    check("Kürzel darf keinen Befehl überschreiben", "schon ein Befehl" in text, text[:80])
    rec.reset()
    say(f"/alias party /play {args.media}/tone500.webm")
    await reply("Kürzel", 10)
    say("/party")
    started = await wait_for(lambda: _state(web)["music"].get("current") and "tone500" in _state(web)["music"]["current"]["title"], 20)
    check("Kürzel auf einen Befehl (/party → /play ...)", bool(started))
    http("POST", f"{web}/api/music/clear")
    http("POST", f"{web}/api/music/skip")
    status, _ = http("PUT", f"{web}/api/aliases", {"name": "bp", "target": "beep"})
    await asyncio.sleep(2)
    rec.reset()
    say("/bp")
    ended = await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 5)
    check("Web: Kürzel anlegen", status == 200 and ended and abs(dominant_frequency(rec.pcm(bot_session)) - 440) < 15)
    rec.reset()
    say("/unalias lr")
    await reply("gelöscht", 10)
    say("/lr")
    text = await reply("Kein Sound", 10)
    check("/unalias löscht das Kürzel", "Kein Sound" in text, text[:60])
    names = [a["name"] for a in extras().get("aliases", [])]
    check("Kürzel stehen im Status", names == ["bp", "party"], str(names))


async def live_scenario(args, web, zasa, rec, bot_session, say, reply) -> None:
    print("Livestreams und Sender ...")
    http("POST", f"{web}/api/music/clear")
    http("POST", f"{web}/api/music/skip")
    await wait_for(lambda: _state(web)["music"].get("current") is None, 10)
    music = lambda: _state(web).get("music", {})  # noqa: E731
    stations = lambda: _state(web).get("extras", {}).get("radio", [])  # noqa: E731
    link = f"{args.media}/live/index.m3u8"

    rec.reset()
    say(f"/radio add lofi {link}")
    text = await reply("gespeichert", 20)
    check("/radio add speichert einen Sender", "gespeichert" in text and [r["name"] for r in stations()] == ["lofi"],
          text[:80])
    status, body = http("PUT", f"{web}/api/radio", {"name": "add", "url": link})
    check("Web: reservierte Sendernamen werden abgelehnt", status == 400, str(body)[:80])

    rec.reset()
    say("/radio lofi")
    on_air = await wait_for(lambda: music().get("state") == "playing", 30, 0.3)
    cur = music().get("current") or {}
    check("/radio lofi startet den Livestream", on_air and cur.get("is_live") is True, str(cur)[:120])
    await asyncio.sleep(4)
    dom = dominant_frequency(rec.pcm(bot_session)[-SAMPLE_RATE * 2:])
    check("Livestream ist im Kanal zu hören (610 Hz)", abs(dom - 610) < 15, f"{dom:.0f} Hz")
    rec.reset()
    say("/np")
    text = await reply("LIVE", 8)
    check("/np zeigt LIVE statt Länge", "LIVE, seit" in text, text[:100])

    # Nobody left in the bot's channel: the livestream pauses on its own (LIVE_IDLE_MINUTES=0.1 here)
    http("POST", f"{web}/api/join", {"channel_id": zasa.find_channel("AFK").id})
    paused = await wait_for(lambda: music().get("state") == "paused", 25, 0.5)
    log = " ".join(a["text"] for a in _state(web).get("activity", [])[-5:])
    check("Livestream pausiert, wenn niemand mehr im Kanal ist", paused and "niemand mehr im Kanal" in log, log[-80:])
    http("POST", f"{web}/api/follow", {"user": "Zasa"})
    await wait_for(lambda: zasa.find_user(BOT_NAME).channel_id == zasa.myself.channel_id, 10)
    rec.reset()
    say("/radio lofi")
    text = await reply("läuft schon", 8)
    resumed = await wait_for(lambda: music().get("state") == "playing", 30, 0.3)
    check("/radio auf den pausierten Sender setzt live fort", resumed and "läuft schon" in text, text[:80])
    heard = await wait_for(lambda: len(rec.audio.get(bot_session, [])) > 50, 10)
    check("Nach dem Fortsetzen wieder zu hören", heard)

    status, body = http("POST", f"{web}/api/radio/gibtsnicht/play")
    check("Web: unbekannter Sender gibt 404", status == 404, str(body)[:80])
    rec.reset()
    say("/skip")
    check("/skip beendet den Livestream", await wait_for(lambda: music().get("current") is None, 10))
    say("/radio del lofi")
    text = await reply("gelöscht", 8)
    check("/radio del löscht den Sender", "gelöscht" in text and stations() == [], text[:80])


async def collections_scenario(args, web, zasa, rec, bot_session, say, reply) -> None:
    print("Sammlungen ...")
    http("POST", f"{web}/api/music/clear")
    http("POST", f"{web}/api/music/skip")
    await asyncio.sleep(2)
    colls = lambda: _state(web).get("collections", [])  # noqa: E731
    loaded = await wait_for(lambda: any(c.get("loaded") for c in colls()), 10, 0.2)
    anno = next((c for c in colls() if c["name"] == "Anno 1800"), {})
    groups = [g["name"] for g in anno.get("groups", [])]
    check("Sammlung wird eingelesen (Sprecher als Untergruppen)", loaded and anno.get("count") == 4
          and groups == ["Bente Jorgensen", "Sir Archibald Blake", "Ohne Zuordnung"], f"{anno.get('count')} {groups}")
    names = [c["name"] for c in colls()]
    check("Original-Ordner hat Vorrang vor einer alten Kopie", names == ["Anno 1800", "Dota 2"] and anno.get("count") == 4,
          str(names))
    dota = next((c for c in colls() if c["name"] == "Dota 2"), {})
    check("Zweite Sammlung Dota 2: Text aus Dateinamen, halbfertige Datei ignoriert",
          dota.get("count") == 3 and dota.get("commands") == ["dota", "dota2"]
          and [g["name"] for g in dota.get("groups", [])] == ["Axe", "Pudge"], str(dota)[:160])
    check("Chat-Befehle für die Sammlung", anno.get("commands") == ["anno", "anno1800"], str(anno.get("commands")))
    base = f"{web}/api/collections/{anno.get('id')}"
    library_before = len(_state(web)["library"]["sounds"])
    tiles = {s["name"] for s in _state(web)["library"]["sounds"]}
    check("Sammlung erscheint nicht als Kacheln", not tiles & {"Wem sagst du das", "Mein Schiff ist schneller",
                                                             "Kennste nicht Kein Witz"}, str(len(tiles)))

    status, body = http("GET", f"{base}/items?q=schiff")
    items = body.get("items", [])
    check("Web: Suche im Text der Zeilen", status == 200 and body.get("total") == 1
          and items[0]["text"] == "Mein Schiff ist schneller." and items[0]["mood"] == "negativ", str(body)[:120])
    status, body = http("GET", f"{base}/items?q=ship%20faster")
    check("Web: Suche auch im englischen Text", body.get("total") == 1, str(body.get("total")))
    status, body = http("GET", f"{base}/items?group=Bente%20Jorgensen&limit=1&offset=1")
    check("Web: Filter nach Sprecher mit Seiten", body.get("total") == 2 and len(body.get("items", [])) == 1, str(body)[:120])
    all_items = http("GET", f"{base}/items")[1]["items"]
    by_text = {i["text"]: i for i in all_items}

    async def played(action, name: str, freq: float) -> None:
        rec.reset()
        action()
        ok = await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 8)
        dom = dominant_frequency(rec.pcm(bot_session))
        check(name, ok and abs(dom - freq) < 15, f"{dom:.0f} Hz" if ok else "nichts gespielt")
        await asyncio.sleep(0.5)

    blake = by_text["Kennste nicht? Kein Witz."]
    await played(lambda: http("POST", f"{base}/items/{blake['id']}/play"), "Web: Zeile im Kanal abspielen (760 Hz)", 760)
    req = urllib.request.urlopen(f"{base}/items/{blake['id']}/audio", timeout=5)
    check("Web: Zeile lokal vorhören (Audiodatei)", req.status == 200 and len(req.read()) > 1000)

    await played(lambda: say("/anno schiff"), "/anno schiff spielt die passende Zeile (620 Hz)", 620)
    text = await reply("Mein Schiff", 5)
    check("/anno zeigt Sprecher und Text im Chat", "Bente Jorgensen" in text and "Mein Schiff ist schneller" in text, text[:80])
    await played(lambda: say("/anno @blake"), "/anno @blake spielt eine Zeile dieses Sprechers (760 Hz)", 760)
    await played(lambda: say("/nochmal"), "/nochmal wiederholt die Sammlungszeile", 760)
    rec.reset()
    say("/anno @lady")
    text = await reply("Niemanden", 5)
    check("/anno @unbekannt meldet sich verständlich", "Niemanden gefunden" in text, text[:80])
    say("/anno gibtsnicht")
    text = await reply("Keine Zeile", 5)
    check("/anno ohne Treffer", "Keine Zeile" in text, text[:80])
    rec.reset()
    status, body = http("POST", f"{base}/random", {"group": "Ohne Zuordnung"})
    check("Web: Zufall in einer Gruppe ohne Text", status == 200 and body.get("item", {}).get("group") == "Ohne Zuordnung"
          and await wait_for(lambda: rec.terminators.get(bot_session, 0) >= 1, 8), str(body)[:100])

    status, body = http("POST", f"{base}/items/{blake['id']}/board")
    sound = body.get("sound", {})
    check("Web: Zeile aufs Board legen", status == 200 and body.get("created") is True
          and sound.get("category") == "Anno 1800" and sound.get("name") == "Kennste nicht Kein Witz"
          and sound.get("speaker") == "Sir Archibald Blake" and sound.get("text") == "Kennste nicht? Kein Witz.",
          str(body)[:200])
    status, body = http("POST", f"{base}/items/{blake['id']}/board")
    check("Web: zweites Mal aufs Board legt keine Kopie an", status == 200 and body.get("created") is False, str(body)[:80])
    lib = _state(web)["library"]["sounds"]
    check("Board hat genau einen Sound mehr", len(lib) == library_before + 1, f"{library_before} -> {len(lib)}")
    await played(lambda: http("POST", f"{web}/api/sounds/{sound.get('id')}/play"), "Kopie spielt wie ein normaler Sound", 760)
    say("/help")
    text = await reply("Sammlungen", 8)
    check("/help listet die Sammlung", "Sammlungen" in text and "anno" in text, text[:80])

    # The export keeps adding files to the original folder: "Neu einlesen" picks them up
    new = args.extern_dir / "Hannah Goode" / "Neue Zeile.mp3"
    new.parent.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=840:duration=1.2", str(new)],
                   check=True)
    status, body = http("POST", f"{web}/api/collections/reload")
    anno = next((c for c in body if c["name"] == "Anno 1800"), {}) if isinstance(body, list) else {}
    check("Neu einlesen findet nachgelieferte Dateien und Sprecher", status == 200 and anno.get("count") == 5
          and "Hannah Goode" in [g["name"] for g in anno.get("groups", [])], str(anno.get("count")))
    await played(lambda: say("/anno neue zeile"), "Nachgelieferte Zeile ist sofort abspielbar (840 Hz)", 840)
    await played(lambda: say("/dota @pudge"), "/dota @pudge spielt eine Pudge-Zeile (680 Hz)", 680)
    text = await reply("Fresh meat", 5)
    check("/dota nennt Held und Zeile", "Pudge" in text and "Fresh meat" in text, text[:80])
    rec.reset()
    say("/dota keen worthy")
    text = await reply("Keen", 5)
    check("Zweitaufnahmen tragen den sauberen Text", "„A Keen worthy to fight“" in text and "(2 Treffer)" in text, text[:100])


def _state(web: str) -> dict:
    try:
        return http("GET", f"{web}/api/state")[1]
    except OSError:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=64738)
    parser.add_argument("--superuser-password", required=True)
    parser.add_argument("--sounds", type=Path, required=True)
    parser.add_argument("--web-port", type=int, default=8098)
    parser.add_argument("--restart-cmd", default="")
    parser.add_argument("--media-port", type=int, default=8766)
    parser.add_argument("--music-only", action="store_true", help="nur die Musik-Tests")
    parser.add_argument("--extras-only", action="store_true", help="nur TTS, Pull-Timer, Join-Sounds und Kürzel")
    parser.add_argument("--collections-only", action="store_true", help="nur die Sammlungen (z. B. Anno-Zeilen)")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
