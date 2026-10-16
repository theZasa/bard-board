"""HTTP API, WebSocket live updates and static web interface."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
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
from .auth import CODE_TTL, SESSION_COOKIE, LoginError, LoginGate, device_label
from .core import PlayError, Soundboard
from .library import AUDIO_EXTENSIONS
from .media import COOKIES_MAX_BYTES, MediaError, format_time, parse_time

log = logging.getLogger("web")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
WEB_USER = "Web"  # who did it, when the web interface needs no login (local setup)


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


class FreshStaticFiles(StaticFiles):
    """The web interface's files, always revalidated by the browser.

    Without a Cache-Control header browsers guess how long to keep a file and may use an old style.css
    with a new script after an update. "no-cache" makes them ask every time; thanks to the ETag an
    unchanged file costs only a tiny 304 answer.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def who(request: Request) -> str:
    """The signed-in person (see auth.LoginGate), or "Web" when no login is needed."""
    return request.scope.get("web_user") or WEB_USER


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
            voice = await board.play(sound, requested_by=who(request), source="web")
        except PlayError as exc:
            return _error(409, str(exc))
        return JSONResponse(voice.to_json())

    async def random_play(request: Request) -> Response:
        body = await _json(request) if await request.body() else {}
        sound = board.random_sound(str(body.get("query", "")))
        if sound is None:
            return _error(404, "Keine Sounds vorhanden")
        try:
            voice = await board.play(sound, requested_by=who(request), source="web")
        except PlayError as exc:
            return _error(409, str(exc))
        return JSONResponse(voice.to_json())

    async def stop(request: Request) -> Response:
        body = await _json(request) if await request.body() else {}
        voice_id = body.get("voice_id")
        if voice_id is None:
            stopped = board.stop_all(requested_by=who(request), source="web")
        else:
            stopped = board.mixer.stop(int(voice_id))
        return JSONResponse({"stopped": stopped})

    async def volume(request: Request) -> Response:
        body = await _json(request)
        try:
            value = int(body["volume"])
        except (KeyError, TypeError, ValueError):
            return _error(400, "volume (0-100) fehlt")
        return JSONResponse({"volume": board.set_volume(value, requested_by=who(request), source="web")})

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
            board.set_follow("", requested_by=who(request), source="web")
        board.join_channel(channel_id)
        return JSONResponse({"ok": True})

    async def follow(request: Request) -> Response:
        body = await _json(request)
        board.set_follow(str(body.get("user", "")).strip(), requested_by=who(request), source="web")
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
        board.log_activity(who(request), "web", f"hat {sound.name} in den Papierkorb verschoben")
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
            board.log_activity(who(request), "web", f"hat {len(added)} Sound(s) hochgeladen")
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
            result = await board.music_add(query, requested_by=who(request), source="web", play_next=bool(body.get("next")))
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
            track = board.music_skip(requested_by=who(request), source="web")
            return JSONResponse({"skipped": track.to_json() if track else None})
        if action == "pause":
            return JSONResponse({"ok": board.music_pause(requested_by=who(request), source="web")})
        if action == "resume":
            return JSONResponse({"ok": board.music_resume(requested_by=who(request), source="web")})
        if action == "clear":
            return JSONResponse({"removed": board.music_clear(requested_by=who(request), source="web")})
        if action == "shuffle":
            board.music_shuffle(requested_by=who(request), source="web")
            return JSONResponse({"ok": True})
        return _error(404, "Unbekannte Aktion")

    async def music_remove(request: Request) -> Response:
        track = board.music_remove(int(request.path_params["tid"]), requested_by=who(request), source="web")
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
        return JSONResponse({"volume": board.music_volume(value, requested_by=who(request), source="web")})

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
                requested_by=who(request), source="web",
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
            voice = await board.speak(text, requested_by=who(request), source="web")
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
            seconds = await board.start_pull(seconds, requested_by=who(request), source="web")
        except PlayError as exc:
            return _error(409, str(exc))
        return JSONResponse({"seconds": seconds, "pull": board.pull})

    async def pull_cancel(request: Request) -> Response:
        return JSONResponse({"cancelled": await board.cancel_pull(requested_by=who(request), source="web")})

    async def joinsound_set(request: Request) -> Response:
        body = await _json(request)
        user = str(body.get("user", "")).strip()
        sound = board.library.get(str(body.get("sound_id", "")))
        if not user:
            return _error(400, "Benutzername fehlt")
        if sound is None:
            return _error(404, "Sound nicht gefunden")
        board.set_join_sound(user, sound, set_by=who(request), source="web")
        return JSONResponse(board.extras_json())

    async def joinsound_delete(request: Request) -> Response:
        if not board.remove_join_sound(request.path_params["user"], set_by=who(request), source="web"):
            return _error(404, "Kein Join-Sound für diesen Benutzer")
        return JSONResponse(board.extras_json())

    async def radio_set(request: Request) -> Response:
        body = await _json(request)
        try:
            await board.radio_set(str(body.get("name", "")), str(body.get("url", "")), created_by=who(request), source="web")
        except (ValueError, MediaError) as exc:
            return _error(400, str(exc))
        return JSONResponse(board.extras_json())

    async def radio_delete(request: Request) -> Response:
        if not board.radio_remove(request.path_params["name"], removed_by=who(request), source="web"):
            return _error(404, "Sender nicht gefunden")
        return JSONResponse(board.extras_json())

    async def radio_play(request: Request) -> Response:
        try:
            station, result = await board.radio_play(request.path_params["name"], requested_by=who(request), source="web")
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
            board.set_alias(name, target, created_by=who(request), source="web")
        except ValueError as exc:
            return _error(400, str(exc))
        return JSONResponse(board.extras_json())

    async def alias_delete(request: Request) -> Response:
        if not board.remove_alias(request.path_params["name"], removed_by=who(request), source="web"):
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
            voice = await board.play_item(collection, item, requested_by=who(request), source="web")
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
            voice = await board.play_item(collection, item, requested_by=who(request), source="web")
        except PlayError as exc:
            return _error(409, str(exc))
        return JSONResponse({"voice": voice.to_json(), "item": item.to_json(), "total": total})

    async def collection_audio(request: Request) -> Response:
        _, item = item_or_404(request)
        return FileResponse(item.path, filename=item.path.name)

    async def collection_to_board(request: Request) -> Response:
        collection, item = item_or_404(request)
        try:
            sound, created = await board.item_to_board(collection, item, requested_by=who(request), source="web")
        except (OSError, ValueError, PlayError) as exc:
            return _error(409, f"Konnte nicht aufs Board legen: {exc}")
        return JSONResponse({"sound": sound.to_json(), "created": created})

    async def http_error(_: Request, exc: HTTPException) -> Response:
        return _error(exc.status_code, exc.detail)

    # ------------------------------------------------------------- YouTube cookies

    async def youtube_cookies(_: Request) -> Response:
        return JSONResponse(board.youtube_cookies_json())

    async def youtube_cookies_set(request: Request) -> Response:
        async with request.form(max_files=1, max_fields=2) as form:
            upload_file = form.get("file")
            if not isinstance(upload_file, UploadFile):
                return _error(400, "Keine Datei empfangen")
            data = await upload_file.read(COOKIES_MAX_BYTES + 1)
        try:
            board.set_youtube_cookies(data, set_by=who(request), source="web")
        except MediaError as exc:
            return _error(422, str(exc))
        return JSONResponse(board.youtube_cookies_json())

    async def youtube_cookies_delete(request: Request) -> Response:
        board.remove_youtube_cookies(removed_by=who(request), source="web")
        return JSONResponse(board.youtube_cookies_json())

    # ------------------------------------------------------------- login (only with WEB_PASSWORD)

    login = board.web_login

    def origin_of(request: Request) -> tuple[str, str, bool]:
        """(origin, WebAuthn RP ID, https?) as the browser sees it, also behind the ingress."""
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
        return f"{proto}://{request.headers.get('host', '')}", request.url.hostname or "", proto == "https"

    def with_session(request: Request, response: Response, token: str) -> Response:
        session = login.session(token)
        response.set_cookie(SESSION_COOKIE, token, max_age=max(1, int(session["expires"] - time.time())), path="/",
                            httponly=True, samesite="lax", secure=origin_of(request)[2])
        return response

    def need_login() -> None:
        if login is None:
            raise HTTPException(404, "Dieses Webinterface braucht keine Anmeldung")

    def need_session(request: Request) -> tuple[str, dict]:
        need_login()
        token = request.scope.get("web_session")
        session = login.session(token)
        if session is None:
            raise HTTPException(401, "Anmeldung erforderlich")
        return token, session

    async def auth_status(request: Request) -> Response:
        if login is None:
            return JSONResponse({"enabled": False})
        session = login.session(request.scope.get("web_session"))
        return JSONResponse({
            "enabled": True,
            "platform": login.platform,
            "prefix": board.cfg.chat_prefix,
            "bot": board.status_json().get("username") or "",
            "user": login.name_of(session["account"]) if session else None,
            "expires": session["expires"] if session else None,
            "passkey": bool(session and session["passkey"]),
        }, headers={"cache-control": "no-store"})

    async def auth_code(_: Request) -> Response:
        need_login()
        flow, code = login.start_code()
        return JSONResponse({"flow": flow, "code": code, "expires_in": CODE_TTL})

    async def auth_code_poll(request: Request) -> Response:
        need_login()
        body = await _json(request)
        state, token = login.poll_code(str(body.get("flow", "")))
        response = JSONResponse({"state": state})
        return with_session(request, response, token) if token else response

    async def passkey_login_options(request: Request) -> Response:
        need_login()
        try:
            flow, options = login.login_options(origin_of(request)[1])
        except LoginError as exc:
            return _error(429, str(exc))
        return JSONResponse({"flow": flow, "options": json.loads(options)})

    async def passkey_login(request: Request) -> Response:
        need_login()
        body = await _json(request)
        origin, rp_id, _ = origin_of(request)
        try:
            token = login.login(str(body.get("flow", "")), body.get("credential") or {}, rp_id, origin)
        except LoginError as exc:
            return _error(401, str(exc))
        board.log_activity(login.name_of(login.session(token)["account"]), "web", "hat sich mit Passkey angemeldet")
        return with_session(request, JSONResponse({"ok": True}), token)

    async def passkey_register_options(request: Request) -> Response:
        token, _ = need_session(request)
        return JSONResponse(json.loads(login.registration_options(token, origin_of(request)[1])))

    async def passkey_register(request: Request) -> Response:
        token, _ = need_session(request)
        body = await _json(request)
        origin, rp_id, _ = origin_of(request)
        try:
            login.register(token, body.get("credential") or {}, rp_id, origin,
                           device_label(request.headers.get("user-agent", "")))
        except LoginError as exc:
            return _error(400, str(exc))
        return with_session(request, JSONResponse({"ok": True}), token)

    async def passkeys(request: Request) -> Response:
        _, session = need_session(request)
        return JSONResponse({"passkeys": login.passkeys_of(session["account"])})

    async def passkey_delete(request: Request) -> Response:
        _, session = need_session(request)
        if not login.delete_passkey(session["account"], request.path_params["cid"]):
            return _error(404, "Passkey nicht gefunden")
        return JSONResponse({"ok": True})

    async def logout(request: Request) -> Response:
        need_login()
        login.end_session(request.scope.get("web_session"))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    auth_routes = [
        Route("/auth/status", auth_status),
        Route("/auth/code", auth_code, methods=["POST"]),
        Route("/auth/code/poll", auth_code_poll, methods=["POST"]),
        Route("/auth/passkey/login-options", passkey_login_options, methods=["POST"]),
        Route("/auth/passkey/login", passkey_login, methods=["POST"]),
        Route("/auth/passkey/register-options", passkey_register_options, methods=["POST"]),
        Route("/auth/passkey/register", passkey_register, methods=["POST"]),
        Route("/auth/passkeys", passkeys),
        Route("/auth/passkeys/{cid}", passkey_delete, methods=["DELETE"]),
        Route("/auth/logout", logout, methods=["POST"]),
    ]

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
        Route("/api/youtube-cookies", youtube_cookies),
        Route("/api/youtube-cookies", youtube_cookies_set, methods=["PUT"]),
        Route("/api/youtube-cookies", youtube_cookies_delete, methods=["DELETE"]),
        *auth_routes,
        WebSocketRoute("/ws", websocket),
        Mount("/", FreshStaticFiles(directory=STATIC_DIR, html=True), name="static"),
    ]
    middleware = [Middleware(LoginGate, login=board.web_login)] if board.web_login else []
    return Starlette(routes=routes, lifespan=lifespan, middleware=middleware,
                     exception_handlers={HTTPException: http_error})
