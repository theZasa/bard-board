"""HTTP API, WebSocket live updates and static web interface."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .audio import DecodeError, decode_file
from .auth import BasicAuth
from .core import PlayError, Soundboard
from .library import AUDIO_EXTENSIONS
from .media import MediaError, format_time, parse_time

log = logging.getLogger("web")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
WEB_USER = "Web"


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "Ungültiges JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON-Objekt erwartet")
    return body


def create_app(board: Soundboard) -> Starlette:
    def sound_or_404(request: Request):
        sound = board.library.get(request.path_params["sid"])
        if sound is None:
            raise HTTPException(404, "Sound nicht gefunden")
        return sound

    async def state(_: Request) -> Response:
        return JSONResponse(board.state_json())

    async def healthz(_: Request) -> Response:
        # Liveness only: a Mumble/Discord outage is reported in the status, a restart would not fix it.
        return JSONResponse({"ok": True, "platform": board.platform.key, "connected": board.platform.ready})

    async def channels(_: Request) -> Response:
        return JSONResponse(board.channels_json())

    async def play(request: Request) -> Response:
        sound = sound_or_404(request)
        try:
            voice = await board.play(sound, requested_by=WEB_USER, source="web")
        except PlayError as exc:
            return _error(409, str(exc))
        return JSONResponse(voice.to_json())

    async def random_play(request: Request) -> Response:
        body = await _json(request) if await request.body() else {}
        sound = board.random_sound(str(body.get("query", "")))
        if sound is None:
            return _error(404, "Keine Sounds vorhanden")
        try:
            voice = await board.play(sound, requested_by=WEB_USER, source="web")
        except PlayError as exc:
            return _error(409, str(exc))
        return JSONResponse(voice.to_json())

    async def stop(request: Request) -> Response:
        body = await _json(request) if await request.body() else {}
        voice_id = body.get("voice_id")
        if voice_id is None:
            stopped = board.stop_all(requested_by=WEB_USER, source="web")
        else:
            stopped = board.mixer.stop(int(voice_id))
        return JSONResponse({"stopped": stopped})

    async def volume(request: Request) -> Response:
        body = await _json(request)
        try:
            value = int(body["volume"])
        except (KeyError, TypeError, ValueError):
            return _error(400, "volume (0-100) fehlt")
        return JSONResponse({"volume": board.set_volume(value, requested_by=WEB_USER, source="web")})

    async def join(request: Request) -> Response:
        body = await _json(request)
        if not board.platform.ready:
            return _error(409, f"Nicht mit {board.platform.name} verbunden")
        try:
            channel_id = board.platform.channel_id_from(body["channel_id"])
        except (KeyError, TypeError, ValueError):
            return _error(400, "channel_id fehlt")
        if not board.platform.has_channel(channel_id):
            return _error(404, "Kanal nicht gefunden")
        if board.follow_user:
            board.set_follow("", requested_by=WEB_USER, source="web")
        board.join_channel(channel_id)
        return JSONResponse({"ok": True})

    async def follow(request: Request) -> Response:
        body = await _json(request)
        board.set_follow(str(body.get("user", "")).strip(), requested_by=WEB_USER, source="web")
        return JSONResponse({"follow_user": board.follow_user})

    async def sound_audio(request: Request) -> Response:
        sound = sound_or_404(request)
        return FileResponse(sound.path, filename=sound.path.name)

    async def update_sound(request: Request) -> Response:
        sound = sound_or_404(request)
        body = await _json(request)
        allowed = {k: body[k] for k in ("volume", "favorite", "name", "category") if k in body}
        try:
            sound = await asyncio.to_thread(board.update_sound, sound, **allowed)
        except (ValueError, OSError) as exc:
            return _error(400, str(exc))
        return JSONResponse(sound.to_json())

    async def delete_sound(request: Request) -> Response:
        sound = sound_or_404(request)
        try:
            await asyncio.to_thread(board.delete_sound, sound)
        except OSError as exc:
            return _error(500, str(exc))
        board.log_activity(WEB_USER, "web", f"hat {sound.name} in den Papierkorb verschoben")
        board.publish_library()
        return JSONResponse({"ok": True})

    async def upload(request: Request) -> Response:
        max_bytes = board.cfg.max_upload_mb * 1024 * 1024
        form = await request.form(max_files=100, max_fields=20)
        category = str(form.get("category") or "").strip()
        added, errors = [], []
        try:
            files = [f for f in form.getlist("files") if isinstance(f, UploadFile)]
            if not files:
                return _error(400, "Keine Dateien empfangen")
            for upload_file in files:
                name = Path(upload_file.filename or "sound").name
                if Path(name).suffix.casefold() not in AUDIO_EXTENSIONS:
                    errors.append(f"{name}: Dateityp nicht unterstützt")
                    continue
                if upload_file.size is not None and upload_file.size > max_bytes:
                    errors.append(f"{name}: größer als {board.cfg.max_upload_mb} MB")
                    continue
                board.cfg.data_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    dir=board.cfg.data_dir, suffix=Path(name).suffix, delete=False
                ) as tmp:
                    tmp_path = Path(tmp.name)
                    while chunk := await upload_file.read(1024 * 1024):
                        tmp.write(chunk)
                try:
                    await decode_file(tmp_path, 1)  # reject files ffmpeg cannot read
                    target = await asyncio.to_thread(board.library.add_file, name, tmp_path, category)
                    added.append(target.stem)
                except DecodeError:
                    errors.append(f"{name}: keine lesbare Audiodatei")
                except (ValueError, OSError) as exc:
                    errors.append(f"{name}: {exc}")
                finally:
                    tmp_path.unlink(missing_ok=True)
        finally:
            await form.close()
        if added:
            await asyncio.to_thread(board.library.scan)
            board.publish_library()
            board.log_activity(WEB_USER, "web", f"hat {len(added)} Sound(s) hochgeladen")
            asyncio.create_task(board.prewarm())
        return JSONResponse({"added": added, "errors": errors}, status_code=200 if added else 400)

    async def websocket(ws: WebSocket) -> None:
        await ws.accept()
        with board.events.subscribe() as queue:
            await ws.send_json({"type": "state", "data": board.state_json()})
            receiver = asyncio.create_task(_drain_incoming(ws))
            try:
                while not receiver.done():
                    getter = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
                    if getter in done:
                        await ws.send_json(getter.result())
                    else:
                        getter.cancel()
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                receiver.cancel()

    async def _drain_incoming(ws: WebSocket) -> None:
        try:
            while True:
                await ws.receive_text()  # clients only send keep-alive pings
        except WebSocketDisconnect:
            return

    # ----------------------------------------------------------------- music

    async def music_add(request: Request) -> Response:
        body = await _json(request)
        query = str(body.get("query", "")).strip()
        if not query:
            return _error(400, "Link oder Suchbegriff fehlt")
        try:
            result = await board.music_add(query, requested_by=WEB_USER, source="web", play_next=bool(body.get("next")))
        except MediaError as exc:
            return _error(422, str(exc))
        return JSONResponse({
            "added": [t.to_json() for t in result.tracks],
            "playlist": result.playlist_title,
            "skipped": result.skipped,
            "position": result.position,
        })

    async def music_action(request: Request) -> Response:
        action = request.path_params["action"]
        if action == "skip":
            track = board.music_skip(requested_by=WEB_USER, source="web")
            return JSONResponse({"skipped": track.to_json() if track else None})
        if action == "pause":
            return JSONResponse({"ok": board.music_pause(requested_by=WEB_USER, source="web")})
        if action == "resume":
            return JSONResponse({"ok": board.music_resume(requested_by=WEB_USER, source="web")})
        if action == "clear":
            return JSONResponse({"removed": board.music_clear(requested_by=WEB_USER, source="web")})
        if action == "shuffle":
            board.music_shuffle(requested_by=WEB_USER, source="web")
            return JSONResponse({"ok": True})
        return _error(404, "Unbekannte Aktion")

    async def music_remove(request: Request) -> Response:
        track = board.music_remove(int(request.path_params["tid"]), requested_by=WEB_USER, source="web")
        if track is None:
            return _error(404, "Titel nicht in der Warteschlange")
        return JSONResponse({"ok": True})

    async def music_move(request: Request) -> Response:
        body = await _json(request)
        try:
            ok = board.music.move(int(body["id"]), int(body["index"]))
        except (KeyError, TypeError, ValueError):
            return _error(400, "id und index fehlen")
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    async def music_volume(request: Request) -> Response:
        body = await _json(request)
        try:
            value = int(body["volume"])
        except (KeyError, TypeError, ValueError):
            return _error(400, "volume (0-100) fehlt")
        return JSONResponse({"volume": board.music_volume(value, requested_by=WEB_USER, source="web")})

    async def clip(request: Request) -> Response:
        body = await _json(request)
        url = str(body.get("url", "")).strip()
        if not url:
            return _error(400, "Link fehlt")
        try:
            start = parse_time(str(body.get("start", "0")))
            end = parse_time(str(body.get("end", "")))
        except ValueError as exc:
            return _error(400, str(exc))
        try:
            sound = await board.create_clip(
                url, start, end,
                name=str(body.get("name", "")).strip(),
                category=body.get("category"),
                requested_by=WEB_USER, source="web",
            )
        except MediaError as exc:
            return _error(422, str(exc))
        return JSONResponse({**sound.to_json(), "range": f"{format_time(start)}-{format_time(end)}"})

    # ----------------------------------------------------------------- extras

    async def tts(request: Request) -> Response:
        body = await _json(request)
        text = str(body.get("text", "")).strip()
        if not text:
            return _error(400, "Text fehlt")
        try:
            voice = await board.speak(text, requested_by=WEB_USER, source="web")
        except PlayError as exc:
            return _error(409, str(exc))
        return JSONResponse(voice.to_json())

    async def pull_start(request: Request) -> Response:
        body = await _json(request) if await request.body() else {}
        try:
            seconds = int(body.get("seconds", 10))
        except (TypeError, ValueError):
            return _error(400, "seconds muss eine Zahl sein")
        try:
            seconds = await board.start_pull(seconds, requested_by=WEB_USER, source="web")
        except PlayError as exc:
            return _error(409, str(exc))
        return JSONResponse({"seconds": seconds, "pull": board.pull})

    async def pull_cancel(_: Request) -> Response:
        return JSONResponse({"cancelled": await board.cancel_pull(requested_by=WEB_USER, source="web")})

    async def joinsound_set(request: Request) -> Response:
        body = await _json(request)
        user = str(body.get("user", "")).strip()
        sound = board.library.get(str(body.get("sound_id", "")))
        if not user:
            return _error(400, "Benutzername fehlt")
        if sound is None:
            return _error(404, "Sound nicht gefunden")
        board.set_join_sound(user, sound, set_by=WEB_USER, source="web")
        return JSONResponse(board.extras_json())

    async def joinsound_delete(request: Request) -> Response:
        if not board.remove_join_sound(request.path_params["user"], set_by=WEB_USER, source="web"):
            return _error(404, "Kein Join-Sound für diesen Benutzer")
        return JSONResponse(board.extras_json())

    async def radio_set(request: Request) -> Response:
        body = await _json(request)
        try:
            await board.radio_set(str(body.get("name", "")), str(body.get("url", "")), created_by=WEB_USER, source="web")
        except (ValueError, MediaError) as exc:
            return _error(400, str(exc))
        return JSONResponse(board.extras_json())

    async def radio_delete(request: Request) -> Response:
        if not board.radio_remove(request.path_params["name"], removed_by=WEB_USER, source="web"):
            return _error(404, "Sender nicht gefunden")
        return JSONResponse(board.extras_json())

    async def radio_play(request: Request) -> Response:
        try:
            station, result = await board.radio_play(request.path_params["name"], requested_by=WEB_USER, source="web")
        except ValueError as exc:
            return _error(404, str(exc))
        except MediaError as exc:
            return _error(409, str(exc))
        return JSONResponse({"station": station, "already": result is None, "music": board.music.to_json()})

    async def alias_set(request: Request) -> Response:
        body = await _json(request)
        name = str(body.get("name", "")).strip().lstrip("/!")
        target = str(body.get("target", "")).strip()
        if not target:
            return _error(400, "Ziel fehlt")
        if not any(target.startswith(p) for p in board.cfg.command_prefixes):
            match = board.library.resolve(target)
            sound = match.unique or (match.sounds[0] if match.sounds and match.exact else None)
            if sound is None:
                return _error(404, f"Kein eindeutiger Sound für \"{target}\"")
            target = sound.name
        try:
            board.set_alias(name, target, created_by=WEB_USER, source="web")
        except ValueError as exc:
            return _error(400, str(exc))
        return JSONResponse(board.extras_json())

    async def alias_delete(request: Request) -> Response:
        if not board.remove_alias(request.path_params["name"], removed_by=WEB_USER, source="web"):
            return _error(404, "Kürzel nicht gefunden")
        return JSONResponse(board.extras_json())

    # ------------------------------------------------------------ collections

    def collection_or_404(request: Request):
        collection = board.catalog.get(request.path_params["cid"])
        if collection is None:
            raise HTTPException(404, "Sammlung nicht gefunden")
        return collection

    def item_or_404(request: Request):
        collection = collection_or_404(request)
        item = collection.get(request.path_params["iid"])
        if item is None:
            raise HTTPException(404, "Zeile nicht gefunden")
        return collection, item

    def _int_param(request: Request, name: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(request.query_params.get(name, default))))
        except ValueError:
            return default

    async def collections(_: Request) -> Response:
        return JSONResponse(board.catalog.to_json())

    async def collections_reload(_: Request) -> Response:
        await board.reload_catalog()
        return JSONResponse(board.catalog.to_json())

    async def collection_items(request: Request) -> Response:
        collection = collection_or_404(request)
        q = request.query_params
        offset = _int_param(request, "offset", 0, 0, 10**7)
        limit = _int_param(request, "limit", 100, 1, 500)
        total, items = collection.search(q.get("q", ""), q.get("group", ""), q.get("mood", ""), offset, limit)
        return JSONResponse({"total": total, "offset": offset, "items": [i.to_json() for i in items],
                             "loaded": collection.loaded})

    async def collection_play(request: Request) -> Response:
        collection, item = item_or_404(request)
        try:
            voice = await board.play_item(collection, item, requested_by=WEB_USER, source="web")
        except PlayError as exc:
            return _error(409, str(exc))
        return JSONResponse({"voice": voice.to_json(), "item": item.to_json()})

    async def collection_random(request: Request) -> Response:
        collection = collection_or_404(request)
        body = await _json(request) if await request.body() else {}
        item, total = collection.pick(str(body.get("q", "")), str(body.get("group", "")), str(body.get("mood", "")))
        if item is None:
            return _error(404, "Keine passende Zeile")
        try:
            voice = await board.play_item(collection, item, requested_by=WEB_USER, source="web")
        except PlayError as exc:
            return _error(409, str(exc))
        return JSONResponse({"voice": voice.to_json(), "item": item.to_json(), "total": total})

    async def collection_audio(request: Request) -> Response:
        _, item = item_or_404(request)
        return FileResponse(item.path, filename=item.path.name)

    async def collection_to_board(request: Request) -> Response:
        collection, item = item_or_404(request)
        try:
            sound, created = await board.item_to_board(collection, item, requested_by=WEB_USER, source="web")
        except (OSError, ValueError, PlayError) as exc:
            return _error(409, f"Konnte nicht aufs Board legen: {exc}")
        return JSONResponse({"sound": sound.to_json(), "created": created})

    async def http_error(_: Request, exc: HTTPException) -> Response:
        return _error(exc.status_code, exc.detail)

    @asynccontextmanager
    async def lifespan(_: Starlette):
        await board.start()
        try:
            yield
        finally:
            await board.stop()

    routes = [
        Route("/healthz", healthz),
        Route("/api/state", state),
        Route("/api/channels", channels),
        Route("/api/sounds/{sid}/play", play, methods=["POST"]),
        Route("/api/sounds/{sid}/audio", sound_audio),
        Route("/api/sounds/{sid}", update_sound, methods=["PATCH"]),
        Route("/api/sounds/{sid}", delete_sound, methods=["DELETE"]),
        Route("/api/random", random_play, methods=["POST"]),
        Route("/api/stop", stop, methods=["POST"]),
        Route("/api/volume", volume, methods=["POST"]),
        Route("/api/join", join, methods=["POST"]),
        Route("/api/follow", follow, methods=["POST"]),
        Route("/api/upload", upload, methods=["POST"]),
        Route("/api/music/add", music_add, methods=["POST"]),
        Route("/api/music/move", music_move, methods=["POST"]),
        Route("/api/music/volume", music_volume, methods=["POST"]),
        Route("/api/music/queue/{tid:int}", music_remove, methods=["DELETE"]),
        Route("/api/music/{action}", music_action, methods=["POST"]),
        Route("/api/clip", clip, methods=["POST"]),
        Route("/api/tts", tts, methods=["POST"]),
        Route("/api/pull", pull_start, methods=["POST"]),
        Route("/api/pull/cancel", pull_cancel, methods=["POST"]),
        Route("/api/joinsounds", joinsound_set, methods=["PUT"]),
        Route("/api/joinsounds/{user}", joinsound_delete, methods=["DELETE"]),
        Route("/api/radio", radio_set, methods=["PUT"]),
        Route("/api/radio/{name}", radio_delete, methods=["DELETE"]),
        Route("/api/radio/{name}/play", radio_play, methods=["POST"]),
        Route("/api/aliases", alias_set, methods=["PUT"]),
        Route("/api/aliases/{name}", alias_delete, methods=["DELETE"]),
        Route("/api/collections", collections),
        Route("/api/collections/reload", collections_reload, methods=["POST"]),
        Route("/api/collections/{cid}/items", collection_items),
        Route("/api/collections/{cid}/random", collection_random, methods=["POST"]),
        Route("/api/collections/{cid}/items/{iid}/play", collection_play, methods=["POST"]),
        Route("/api/collections/{cid}/items/{iid}/audio", collection_audio),
        Route("/api/collections/{cid}/items/{iid}/board", collection_to_board, methods=["POST"]),
        WebSocketRoute("/ws", websocket),
        Mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static"),
    ]
    middleware = [Middleware(BasicAuth, password=board.cfg.web_password)] if board.cfg.web_password else []
    return Starlette(routes=routes, lifespan=lifespan, middleware=middleware,
                     exception_handlers={HTTPException: http_error})
