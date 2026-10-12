"""The soundboard service: glues the voice platform (Mumble or Discord), mixer, library and web together."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .audio import AudioCache, DecodeError
from .catalog import Collection, Collections, Item
from .config import Config
from .events import EventHub
from .extras import Aliases, JoinSounds, RadioStations
from .library import Library, Sound, fold, safe_category, safe_filename, sound_id
from .media import MediaError, Resolver, format_time, is_url, make_clip
from .mixer import Mixer, Voice
from .music import AddResult, MusicPlayer, Track
from .opus import SAMPLE_RATE
from .platforms import make_platform
from .platforms.base import ChannelId, IncomingText, PlatformUser
from .tts import Speech, TTSError, beep, war_horn

log = logging.getLogger("soundboard")

SCAN_INTERVAL = 4.0
CATALOG_RESCAN = 10 * 60
CATALOG_FULL_EVERY = 6  # every sixth rescan (hourly) ignores the per-folder shortcut
JOIN_SOUND_DELAY = 1.0  # give the joining client a moment before the sound starts
COUNT_WORDS = {1: "Eins", 2: "Zwei", 3: "Drei", 4: "Vier", 5: "Fünf"}


def sound_id_for(path: Path, root: Path) -> str:
    return sound_id(path.relative_to(root).as_posix())


class PlayError(RuntimeError):
    pass


@dataclass
class Activity:
    ts: float
    user: str
    source: str  # "mumble" | "discord" | "web" | "system" | ...
    text: str

    def to_json(self) -> dict:
        return {"ts": self.ts, "user": self.user, "source": self.source, "text": self.text}


class Soundboard:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.events = EventHub()
        self.library = Library(cfg.sounds_dir, cfg.library_file, cfg.trash_dir, cfg.ignore_dirs)
        self.cache = AudioCache(
            cfg.cache_dir, normalize=cfg.normalize, trim=cfg.trim_silence, max_seconds=cfg.max_sound_seconds
        )
        # Collections are decoded on demand and only kept in memory: caching thousands of voice lines
        # on disk would cost gigabytes for lines that are played once.
        self.catalog = Collections(list(cfg.collections_dirs))
        self.catalog_cache = AudioCache(
            cfg.cache_dir, normalize=cfg.normalize, trim=cfg.trim_silence, max_seconds=cfg.max_sound_seconds,
            memory_budget=64 * 1024 * 1024, disk=False,
        )
        self.settings = self._load_settings()
        self.platform = make_platform(cfg)
        self.platform.bind(self)
        self.resolver = Resolver(cfg.ytdlp_cache_dir, max_playlist=cfg.max_playlist_items)
        self.music = MusicPlayer(
            self.resolver,
            state_file=cfg.music_file,
            volume=cfg.music_default_volume,
            max_seconds=cfg.max_track_minutes * 60,
            normalize=cfg.music_normalize,
            on_change=self._schedule_music_update,
            on_notice=self._music_notice,
            on_active=lambda: self.mixer.wake(),
            on_track=self._music_track_started,
        )
        self.mixer = Mixer(
            self.platform.audio,
            max_voices=cfg.max_voices,
            overlap=cfg.overlap,
            volume=int(self.settings.get("volume", cfg.default_volume)),
            on_change=self._schedule_playing_update,
            music=self.music,
            duck_level=cfg.music_duck_volume / 100,
        )
        self.joinsounds = JoinSounds(cfg.joinsounds_file)
        self.aliases = Aliases(cfg.aliases_file)
        self.radio = RadioStations(cfg.radio_file)
        self._live_idle_since: float | None = None
        self.speech = Speech(cfg.tts_model, max_chars=cfg.tts_max_chars)
        self.pull: dict | None = None
        self._pull_task: asyncio.Task | None = None
        self._horn = None
        self._join_last: dict[str, float] = {}
        self.activity: deque[Activity] = deque(maxlen=100)
        self.last_sound_id: str | None = None
        self.last_played: Sound | None = None
        self._tasks: list[asyncio.Task] = []
        self._playing_update_pending = False
        self._music_update_pending = False
        self._command_tasks: set[asyncio.Task] = set()
        self._meta_dirty = False
        self._desired_channel: ChannelId | None = None
        self._move_attempts = 0
        self._last_move: tuple[ChannelId | None, float] = (None, 0.0)
        self._reconcile_event = asyncio.Event()
        # Imported lazily to avoid a circular import (commands needs Soundboard types).
        from .commands import CommandHandler

        self.commands = CommandHandler(self)

    # ---------------------------------------------------------------- settings

    def _load_settings(self) -> dict:
        try:
            return json.loads(self.cfg.settings_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.cfg.settings_file)

    @property
    def follow_user(self) -> str:
        """Runtime override from /follow wins over MUMBLE_FOLLOW_USER / DISCORD_FOLLOW_USER."""
        return self.settings.get("follow_user", self.cfg.follow_user_default) or ""

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self.events.bind(asyncio.get_running_loop())
        self.library.scan()
        self._flatten_collection_copies()
        self.mixer.start()
        self.music.start()
        self._tasks = [
            asyncio.create_task(self.platform.run_forever(), name=self.platform.key),
            asyncio.create_task(self._watch_library(), name="library-watch"),
            asyncio.create_task(self.prewarm(), name="prewarm"),
            asyncio.create_task(self._flush_meta_loop(), name="meta-flush"),
            asyncio.create_task(self._channel_loop(), name="channel"),
            asyncio.create_task(self.speech.warm_up(), name="tts-warmup"),
            asyncio.create_task(self._catalog_loop(), name="catalog"),
        ]
        self.log_activity("System", "system", "Soundboard gestartet")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.music.close()
        await self.mixer.close()
        await self.platform.close()
        self.library.save_meta()

    async def _watch_library(self) -> None:
        # Polling instead of inotify: file events do not cross Docker Desktop bind mounts on Windows.
        while True:
            await asyncio.sleep(SCAN_INTERVAL)
            try:
                if await asyncio.to_thread(self.library.scan):
                    self.publish_library()
                    asyncio.create_task(self.prewarm())
            except Exception:  # noqa: BLE001
                log.exception("Fehler beim Einlesen der Sounds")

    async def prewarm(self) -> None:
        """Decode every sound in the background so playback starts instantly."""
        changed = False
        for sound in list(self.library.sounds.values()):
            if sound.duration is not None and self.cache.is_cached(sound.path):
                continue
            try:
                pcm = await self.cache.get(sound.path)
                sound.duration = round(pcm.size / SAMPLE_RATE, 2)
                changed = True
            except (DecodeError, OSError) as exc:
                log.warning("Kann %s nicht dekodieren: %s", sound.rel, exc)
        if changed:
            self.library.save_meta()
            self.publish_library()
        removed = await asyncio.to_thread(self.cache.prune, [s.path for s in self.library.sounds.values()])
        if removed:
            log.info("%d veraltete Cache-Dateien entfernt", removed)

    async def _flush_meta_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            if self._meta_dirty:
                self._meta_dirty = False
                try:
                    await asyncio.to_thread(self.library.save_meta)
                except OSError:
                    log.exception("Metadaten konnten nicht gespeichert werden")

    # ---------------------------------------------------------------- actions

    async def play(self, sound: Sound, *, requested_by: str, source: str, note: str | None = None,
                   cache: AudioCache | None = None) -> Voice:
        self._check_audio()
        try:
            pcm = await (cache or self.cache).get(sound.path)
        except FileNotFoundError as exc:
            raise PlayError(f"Datei fehlt: {sound.rel}") from exc
        except DecodeError as exc:
            raise PlayError(f"{sound.name} kann nicht abgespielt werden: {exc}") from exc
        sound.duration = round(pcm.size / SAMPLE_RATE, 2)
        voice = self.mixer.play(
            sound.id, sound.name, pcm, gain=sound.volume / 100, requested_by=requested_by, source=source
        )
        self.last_sound_id = sound.id
        self.last_played = sound
        self.log_activity(requested_by, source, note or f"spielt {sound.name}")
        if self.library.sounds.get(sound.id) is sound:  # collection lines are not part of the library
            self.library.record_play(sound)
            self._meta_dirty = True
            self.events.publish("sound", sound.to_json())
        return voice

    # ------------------------------------------------------------ collections

    async def _catalog_loop(self) -> None:
        """Read the collections at startup and again every few minutes. Rescans only look into folders
        that changed; once an hour everything is read again to be safe."""
        rounds = 0
        while True:
            full = rounds % CATALOG_FULL_EVERY == 0
            if rounds:
                await self._discover_collections()
            for collection in self.catalog:
                before = len(collection.items)
                try:
                    await asyncio.to_thread(collection.load, full=full)
                except Exception:  # noqa: BLE001 - a broken collection must not stop the bot
                    log.exception("Sammlung %s konnte nicht gelesen werden", collection.name)
                if rounds == 0 or len(collection.items) != before:
                    self.events.publish("collections", self.catalog.to_json())
            rounds += 1
            await asyncio.sleep(CATALOG_RESCAN)

    async def _discover_collections(self) -> None:
        """New or removed collection folders (e.g. a freshly filled volume) without a restart."""
        if not await asyncio.to_thread(self.catalog.discover):
            return
        log.info("Sammlungen geändert: %s", ", ".join(c.name for c in self.catalog) or "keine")
        self.commands.refresh_collections()
        changed = getattr(self.platform, "commands_changed", None)
        if changed is not None:
            changed()
        self.events.publish("collections", self.catalog.to_json())

    async def reload_catalog(self) -> None:
        await self._discover_collections()
        for collection in self.catalog:
            await asyncio.to_thread(collection.load)
        self.events.publish("collections", self.catalog.to_json())

    async def play_item(self, collection: Collection, item: Item, *, requested_by: str, source: str) -> Voice:
        sound = collection.as_sound(item)
        speaker = item.who if item.group != collection.name else collection.name
        return await self.play(sound, requested_by=requested_by, source=source, cache=self.catalog_cache,
                               note=f"spielt {speaker}: {item.title}")

    async def item_to_board(self, collection: Collection, item: Item, *, requested_by: str, source: str
                            ) -> tuple[Sound, bool]:
        """Copy a collection line onto the board, all into one category named after the collection; the
        speaker is kept as a note on the tile. Returns (sound, created)."""
        category = safe_category(collection.name)
        filename = safe_filename(collection.board_filename(item))
        speaker = item.who if item.group != collection.name else item.speaker
        root = self.library.sounds_dir
        size = item.path.stat().st_size
        for existing in self.library.sounds.values():  # the same line again (also under a "(2)" name)
            if existing.category == category and existing.size == size and existing.speaker == speaker \
                    and fold(existing.text or existing.name) == fold(item.text or Path(filename).stem):
                return existing, False

        def copy() -> Path:
            self.cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=self.cfg.tmp_dir, suffix=item.path.suffix)
            os.close(fd)
            try:
                shutil.copyfile(item.path, tmp_name)
                path = self.library.add_file(filename, Path(tmp_name), category)
            finally:
                Path(tmp_name).unlink(missing_ok=True)
            self.library.scan()
            return path

        path = await asyncio.to_thread(copy)
        sound = self.library.get(sound_id_for(path, root))
        if sound is None:
            raise PlayError("Kopie wurde nicht gefunden")
        sound.speaker = speaker
        sound.text = item.text if item.text != sound.name else ""
        await asyncio.to_thread(self.library.save_meta)
        self.publish_library()
        asyncio.create_task(self.prewarm())
        self.log_activity(requested_by, source, f"legt aufs Board: {sound.name}")
        return sound, True

    def random_sound(self, query: str = "") -> Sound | None:
        pool = self.library.search(query, limit=100000) if query else list(self.library.sounds.values())
        return random.choice(pool) if pool else None

    def stop_all(self, *, requested_by: str, source: str) -> int:
        """Stop all sounds and pause the music (the queue stays)."""
        stopped = self.mixer.stop()
        if self.music.current is not None and self.music.pause():
            stopped += 1
        if stopped:
            self.log_activity(requested_by, source, "hat gestoppt")
        return stopped

    # ------------------------------------------------------------------ music

    async def music_add(self, query: str, *, requested_by: str, source: str, play_next: bool = False) -> AddResult:
        result = await self.music.add(query, requested_by=requested_by, source=source, play_next=play_next)
        if result.playlist_title or len(result.tracks) > 1:
            text = f"fügt Playlist {result.playlist_title or ''} hinzu ({len(result.tracks)} Titel)".replace("  ", " ")
        else:
            text = f"fügt {result.tracks[0].info.title} hinzu"
        self.log_activity(requested_by, source, text)
        return result

    def music_skip(self, *, requested_by: str, source: str) -> Track | None:
        track = self.music.skip()
        if track:
            self.log_activity(requested_by, source, f"überspringt {track.info.title}")
        return track

    def music_pause(self, *, requested_by: str, source: str) -> bool:
        if self.music.pause():
            self.log_activity(requested_by, source, "pausiert die Musik")
            return True
        return False

    def music_resume(self, *, requested_by: str, source: str) -> bool:
        if self.music.resume():
            self.log_activity(requested_by, source, "setzt die Musik fort")
            return True
        return False

    def music_clear(self, *, requested_by: str, source: str) -> int:
        count = self.music.clear()
        if count:
            self.log_activity(requested_by, source, f"leert die Warteschlange ({count} Titel)")
        return count

    def music_shuffle(self, *, requested_by: str, source: str) -> None:
        self.music.shuffle()
        self.log_activity(requested_by, source, "mischt die Warteschlange")

    def music_remove(self, track_id: int, *, requested_by: str, source: str) -> Track | None:
        track = self.music.remove(track_id)
        if track:
            self.log_activity(requested_by, source, f"entfernt {track.info.title}")
        return track

    def music_volume(self, volume: int, *, requested_by: str, source: str) -> int:
        value = self.music.set_volume(volume)
        self.log_activity(requested_by, source, f"Musik-Lautstärke {value} %")
        return value

    async def create_clip(
        self,
        url: str,
        start: float,
        end: float,
        *,
        name: str = "",
        category: str | None = None,
        requested_by: str,
        source: str,
    ) -> Sound:
        if end <= start:
            raise MediaError("Das Ende muss nach dem Start liegen")
        if end - start > self.cfg.max_clip_seconds:
            raise MediaError(f"Clips dürfen höchstens {self.cfg.max_clip_seconds} Sekunden lang sein")
        if not name:
            lookup = await self.resolver.lookup(url)
            name = f"{lookup.tracks[0].info.title[:60]} {format_time(start)}".replace(":", ".")
        try:
            filename = safe_filename(f"{name.strip()}.mp3")
            folder = safe_category(category if category is not None else self.cfg.clip_category)
        except ValueError as exc:
            raise MediaError(str(exc)) from exc
        directory = self.cfg.sounds_dir / folder if folder else self.cfg.sounds_dir
        target = self.library._unique_target(directory, filename)
        await make_clip(self.resolver, url, start, end, target, self.cfg.tmp_dir)
        await asyncio.to_thread(self.library.scan)
        self.publish_library()
        sound = next((s for s in self.library.sounds.values() if s.path == target), None)
        if sound is None:
            raise MediaError("Clip wurde gespeichert, aber nicht gefunden")
        self.log_activity(requested_by, source, f"speichert Clip {sound.name}")
        asyncio.create_task(self.prewarm())
        return sound

    def _music_track_started(self, track: Track) -> None:
        if not self.cfg.music_announce:
            return
        rich = getattr(self.platform, "announce_track", None)
        if rich is not None:  # Discord: a "now playing" card instead of a text line
            rich(track.info, track.requested_by)
            return
        title = html.escape(track.info.title, quote=False)
        if track.info.webpage.startswith(("http://", "https://")):
            title = f'<a href="{html.escape(track.info.webpage)}">{title}</a>'
        if track.info.is_live:
            length = " (LIVE)"
        else:
            length = f" ({format_time(track.info.duration)})" if track.info.duration else ""
        who = html.escape(track.requested_by, quote=False)
        self.platform.announce(f"&#127925; Jetzt: {title}{length} <i>- {who}</i>")

    def _music_notice(self, text: str) -> None:
        self.log_activity("Musik", "system", text)
        self.say(text)

    def say(self, text: str) -> None:
        """Post a plain text message into the bot's channel."""
        self.platform.announce(html.escape(text, quote=False))

    def _schedule_music_update(self) -> None:
        if self._music_update_pending:
            return
        self._music_update_pending = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._music_update_pending = False
            return

        def emit() -> None:
            self._music_update_pending = False
            self.events.publish("music", self.music.to_json())

        loop.call_soon(emit)

    def set_volume(self, volume: int, *, requested_by: str, source: str) -> int:
        self.mixer.set_volume(volume)
        self.settings["volume"] = self.mixer.volume
        self._save_settings()
        self.log_activity(requested_by, source, f"Lautstärke {self.mixer.volume} %")
        self.events.publish("volume", self.mixer.volume)
        return self.mixer.volume

    def set_follow(self, name: str, *, requested_by: str, source: str) -> None:
        here = self.platform.channel_id
        if not name and here is not None:
            self._desired_channel = here  # stay where we are
        self.settings["follow_user"] = name
        self._save_settings()
        self.log_activity(requested_by, source, f"Bot folgt {name}" if name else "Bot folgt niemandem mehr")
        self._reconcile_event.set()
        self.publish_status()

    def join_channel(self, channel_id: ChannelId) -> None:
        self._want_channel(channel_id)

    # Channel handling: the desired channel is reconciled continuously, because servers may drop
    # move requests (rate limiting) and followed users can hop channels quickly.

    def _want_channel(self, channel_id: ChannelId | None) -> None:
        if channel_id != self._desired_channel:
            self._move_attempts = 0
        self._desired_channel = channel_id
        self._reconcile_event.set()

    def _target_channel(self) -> ChannelId | None:
        follow = self.follow_user
        if follow:
            user = self.platform.find_user(follow)
            if user and user.channel_id is not None:
                return user.channel_id
        return self._desired_channel

    def _reconcile_channel(self) -> None:
        p = self.platform
        if not p.ready:
            return
        here = p.channel_id
        target = self._target_channel()
        if target is None or not p.has_channel(target) or target == here:
            if target == here:
                self._move_attempts = 0
            return
        if target != self._last_move[0]:
            self._move_attempts = 0
        now = time.monotonic()
        if p.moving or (self._last_move[0] == target and now - self._last_move[1] < 1.5):
            return  # a request is queued or still in flight
        if self._move_attempts >= 5:
            if self._move_attempts == 5:
                log.warning("Kanalwechsel nach %s klappt nicht (fehlende Rechte?)", p.channel_path(target))
                self._move_attempts += 1
            return
        self._move_attempts += 1
        self._last_move = (target, now)
        log.info("Wechsle in Kanal %s", p.channel_path(target))
        p.move_to(target)

    async def _channel_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._reconcile_event.wait(), 1.0)
            except (TimeoutError, asyncio.TimeoutError):
                pass
            self._reconcile_event.clear()
            try:
                self._reconcile_channel()
                self._check_live_idle()
            except ConnectionError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("Fehler beim Kanalwechsel")

    def update_sound(self, sound: Sound, **changes) -> Sound:
        if "volume" in changes:
            sound.volume = max(0, min(200, int(changes["volume"])))
        if "favorite" in changes:
            sound.favorite = bool(changes["favorite"])
        if changes.get("name") is not None or changes.get("category") is not None:
            old_rel = sound.rel
            sound = self.library.rename(sound, changes.get("name"), changes.get("category"))
            if sound.rel != old_rel:
                self.joinsounds.sound_moved(old_rel, sound.rel)
                self.publish_extras()
        self.library.save_meta()
        self.publish_library()
        return sound

    def _flatten_collection_copies(self) -> None:
        """One-time move: lines copied from a collection used to land in "<Sammlung>/<Sprecher>", now all
        of them share "<Sammlung>" and the speaker is a note on the tile. Plays, favorites, volume and join
        sounds move along (same path as a rename in the web interface)."""
        if self.settings.get("collection_copies_flat"):
            return
        moved = 0
        for collection in self.catalog:
            prefix = f"{collection.name}/"
            for sound in [s for s in self.library.sounds.values() if s.category.startswith(prefix)]:
                speaker = sound.category[len(prefix):]
                try:
                    sound = self.update_sound(sound, category=collection.name)
                except (OSError, ValueError) as exc:
                    log.warning("Konnte %s nicht nach %s verschieben: %s", sound.rel, collection.name, exc)
                    continue
                if not sound.speaker:
                    sound.speaker = speaker
                moved += 1
        if moved:
            self.library.save_meta()
            log.info("%d Sammlungs-Sounds in ihre gemeinsame Kategorie verschoben", moved)
        self.settings["collection_copies_flat"] = True
        self._save_settings()

    def delete_sound(self, sound: Sound) -> None:
        self.library.delete(sound)
        self.joinsounds.sound_moved(sound.rel, None)
        self.publish_extras()

    # --------------------------------------------- join sounds, aliases, speech

    def _canonical_user(self, name: str) -> str:
        """Spell a user name like the platform does (or like the existing entry) instead of how it was typed."""
        online = self.platform.find_user(name)
        if online is not None:
            return online.name
        entry = self.joinsounds.entries.get(name.casefold())
        return entry["user"] if entry else name

    def set_join_sound(self, user: str, sound: Sound, *, set_by: str, source: str) -> str:
        user = self._canonical_user(user)
        self.joinsounds.set(user, sound.rel, set_by)
        # A new sound should be heard on the next join, not only after the cooldown of the old one.
        self._join_last.pop(user.casefold(), None)
        self.log_activity(set_by, source, f"Join-Sound für {user}: {sound.name}")
        self.publish_extras()
        return user

    def remove_join_sound(self, user: str, *, set_by: str, source: str) -> bool:
        user = self._canonical_user(user)
        removed = self.joinsounds.remove(user)
        self._join_last.pop(user.casefold(), None)
        if removed:
            self.log_activity(set_by, source, f"Join-Sound für {user} entfernt")
            self.publish_extras()
        return removed

    def join_sound_for(self, user: str) -> Sound | None:
        rel = self.joinsounds.get(user)
        return next((s for s in self.library.sounds.values() if s.rel == rel), None) if rel else None

    async def _play_join_sound(self, user: PlatformUser) -> None:
        sound = self.join_sound_for(user.name)
        if sound is None:
            return
        key = user.name.casefold()
        now = time.monotonic()
        if now - self._join_last.get(key, -1e9) < self.cfg.join_sound_cooldown:
            return
        self._join_last[key] = now  # set before the delay so a quick second join cannot play it twice
        await asyncio.sleep(JOIN_SOUND_DELAY)
        current = self.platform.user(user.id)
        if current is None or current.channel_id is None or current.channel_id != self.platform.channel_id:
            self._join_last.pop(key, None)  # left again in the meantime: nothing played, no cooldown
            return
        try:
            await self.play(sound, requested_by=user.name, source="join", note=f"kommt rein: {sound.name}")
        except PlayError as exc:
            log.warning("Join-Sound für %s: %s", user.name, exc)

    def set_alias(self, name: str, target: str, *, created_by: str, source: str) -> str:
        reserved = {n for cmd in self.commands.commands for n in cmd.names}
        key = self.aliases.validate_name(name, reserved)
        if not target.strip():
            raise ValueError("Ziel fehlt")
        self.aliases.set(key, target, created_by)
        self.log_activity(created_by, source, f"Kürzel /{key} → {target.strip()}")
        self.publish_extras()
        return key

    def remove_alias(self, name: str, *, removed_by: str, source: str) -> bool:
        removed = self.aliases.remove(name)
        if removed:
            self.log_activity(removed_by, source, f"Kürzel /{name.casefold()} entfernt")
            self.publish_extras()
        return removed

    def _check_audio(self) -> None:
        if not self.platform.ready:
            raise PlayError(f"Der Bot ist gerade nicht mit {self.platform.name} verbunden")
        if not self.platform.audio.ready:
            raise PlayError("Der Bot ist in keinem Sprachkanal")

    def _play_pcm(self, key: str, name: str, pcm, *, requested_by: str, source: str) -> Voice:
        self._check_audio()
        return self.mixer.play(key, name, pcm, gain=1.0, requested_by=requested_by, source=source)

    async def speak(self, text: str, *, requested_by: str, source: str) -> Voice:
        self._check_audio()
        try:
            pcm = await self.speech.speak(text)
        except TTSError as exc:
            raise PlayError(str(exc)) from exc
        short = text if len(text) <= 60 else text[:57] + "..."
        voice = self._play_pcm("tts", f"🗣 {short}", pcm, requested_by=requested_by, source=source)
        self.log_activity(requested_by, source, f"sagt: {short}")
        return voice

    # ------------------------------------------------------------- pull timer

    async def _pull_clip(self, text: str, fallback):
        if self.speech.available:
            try:
                return await self.speech.speak(text)
            except TTSError as exc:
                log.warning("Pull-Timer ohne Sprache: %s", exc)
        return fallback

    async def _horn_pcm(self):
        if self.cfg.pull_sound:
            match = self.library.resolve(self.cfg.pull_sound)
            sound = match.unique or (match.sounds[0] if match.sounds and match.exact else None)
            if sound is not None:
                try:
                    return await self.cache.get(sound.path)
                except (DecodeError, OSError) as exc:
                    log.warning("PULL_SOUND %s nicht nutzbar: %s", sound.name, exc)
            else:
                log.warning("PULL_SOUND %r nicht gefunden, nutze das Horn", self.cfg.pull_sound)
        if self._horn is None:
            self._horn = await asyncio.to_thread(war_horn)
        return self._horn

    async def start_pull(self, seconds: int, *, requested_by: str, source: str) -> int:
        self._check_audio()
        seconds = max(3, min(int(seconds), self.cfg.pull_max_seconds))
        await self.cancel_pull(requested_by=requested_by, source=source, announce=False)
        # Synthesize everything first, so the countdown itself is exact.
        top = min(5, seconds)
        numbers = {n: await self._pull_clip(COUNT_WORDS[n], beep(880.0)) for n in range(top, 0, -1)}
        intro = await self._pull_clip(f"Pull in {seconds} Sekunden", None)
        final = await self._pull_clip("Pull!", None)
        horn = await self._horn_pcm()
        ends_at = time.time() + seconds
        self.pull = {"seconds": seconds, "ends_at": ends_at, "by": requested_by}
        self._pull_task = asyncio.create_task(self._run_pull(seconds, intro, numbers, final, horn, requested_by))
        self.log_activity(requested_by, source, f"Pull-Timer {seconds} s")
        self.say(f"⏱ Pull in {seconds} Sekunden ({requested_by})")
        self.publish_extras()
        return seconds

    async def _run_pull(self, seconds, intro, numbers, final, horn, requested_by: str) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()

        async def at(offset: float) -> None:
            delay = start + offset - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)

        try:
            top = max(numbers)
            if intro is not None and (seconds - top) >= intro.size / SAMPLE_RATE + 0.3:
                self._play_pcm("pull", f"⏱ Pull in {seconds}", intro, requested_by=requested_by, source="pull")
            for n in sorted(numbers, reverse=True):
                await at(seconds - n)
                self._play_pcm("pull", f"⏱ {n}", numbers[n], requested_by=requested_by, source="pull")
            await at(seconds)
            if final is not None:
                self._play_pcm("pull", "⏱ Pull!", final, requested_by=requested_by, source="pull")
            self._play_pcm("pull", "📯 Horn", horn, requested_by=requested_by, source="pull")
        except PlayError as exc:
            log.warning("Pull-Timer abgebrochen: %s", exc)
        finally:
            if self._pull_task is asyncio.current_task():
                self.pull = None
                self._pull_task = None
                self.publish_extras()

    async def cancel_pull(self, *, requested_by: str, source: str, announce: bool = True) -> bool:
        task, self._pull_task = self._pull_task, None
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.pull = None
        self.mixer.stop_matching(lambda v: v.sound_id == "pull")
        if announce:
            self.log_activity(requested_by, source, "bricht den Pull-Timer ab")
            self.say(f"⏱ Pull abgebrochen ({requested_by})")
            if self.speech.available and self.platform.audio.ready:
                try:
                    pcm = await self.speech.speak("Pull abgebrochen")
                    self._play_pcm("pull", "⏱ Pull abgebrochen", pcm, requested_by=requested_by, source="pull")
                except (TTSError, PlayError):
                    pass
        self.publish_extras()
        return True

    def extras_json(self) -> dict:
        joins = []
        for entry in self.joinsounds.entries.values():
            sound = next((s for s in self.library.sounds.values() if s.rel == entry["sound"]), None)
            joins.append({"user": entry["user"], "sound_id": sound.id if sound else None,
                          "sound": sound.name if sound else entry["sound"], "missing": sound is None})
        tts_problem = self.speech.unavailable_reason
        return {
            "joinsounds": sorted(joins, key=lambda j: j["user"].casefold()),
            "aliases": sorted(self.aliases.entries.values(), key=lambda a: a["name"]),
            "radio": sorted(self.radio.entries.values(), key=lambda r: r["name"]),
            "pull": self.pull,
            "tts": {"available": not tts_problem, "reason": tts_problem},
        }

    # ------------------------------------------------------------- livestreams

    def _check_live_idle(self) -> None:
        """Pause a livestream once nobody but the bot has been in the channel for LIVE_IDLE_MINUTES."""
        limit = self.cfg.live_idle_minutes * 60
        track = self.music.current
        here = self.platform.channel_id
        if not limit or track is None or not track.info.is_live or self.music.paused or here is None \
                or self.platform.users_in_channel(here):
            self._live_idle_since = None
            return
        now = time.monotonic()
        if self._live_idle_since is None:
            self._live_idle_since = now
        elif now - self._live_idle_since >= limit:
            self._live_idle_since = None
            if self.music.pause():
                self.log_activity("Musik", "system", f"pausiert {track.info.title}, niemand mehr im Kanal")

    async def radio_set(self, name: str, url: str, *, created_by: str, source: str) -> dict:
        key = RadioStations.validate_name(name.strip())
        url = url.strip()
        if not is_url(url):
            raise ValueError("Bitte einen Link zum Livestream angeben")
        lookup = await self.music.resolver.lookup(url)
        if len(lookup.tracks) != 1:
            raise ValueError("Bitte einen einzelnen Stream verlinken, keine Playlist")
        info = lookup.tracks[0]
        self.radio.set(key, info.webpage or url, info.title, created_by)
        self.log_activity(created_by, source, f"speichert Sender {key}: {info.title}")
        self.publish_extras()
        return self.radio.get(key)

    def radio_remove(self, name: str, *, removed_by: str, source: str) -> bool:
        removed = self.radio.remove(name)
        if removed:
            self.log_activity(removed_by, source, f"löscht Sender {name.casefold()}")
            self.publish_extras()
        return removed

    async def radio_play(self, name: str, *, requested_by: str, source: str) -> tuple[dict, AddResult | None]:
        """Play a saved station next. Switching from one livestream to another happens right away.
        Returns (station, None) when that station is already on air."""
        station = self.radio.get(name)
        if station is None:
            raise ValueError(f"Keinen Sender \"{name}\" gefunden")
        current = self.music.current
        if current is not None and current.info.webpage == station["url"]:
            if self.music.paused:
                self.music_resume(requested_by=requested_by, source=source)
            return station, None
        result = await self.music_add(station["url"], requested_by=requested_by, source=source, play_next=True)
        if current is not None and current.info.is_live and self.music.current is current:
            self.music.skip()  # zapping between stations
        if self.music.paused:
            self.music_resume(requested_by=requested_by, source=source)  # /radio means: I want to listen now
        return station, result

    def publish_extras(self) -> None:
        self.events.publish("extras", self.extras_json())

    # ------------------------------------------------------------- reporting

    def log_activity(self, user: str, source: str, text: str) -> None:
        entry = Activity(time.time(), user, source, text)
        self.activity.append(entry)
        self.events.publish("activity", entry.to_json())

    def _schedule_playing_update(self) -> None:
        if self._playing_update_pending:
            return
        self._playing_update_pending = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._playing_update_pending = False
            return

        def emit() -> None:
            self._playing_update_pending = False
            self.events.publish("playing", self.playing_json())

        loop.call_soon(emit)

    def playing_json(self) -> list[dict]:
        return [v.to_json() for v in self.mixer.voices]

    def status_json(self) -> dict:
        p = self.platform
        here = p.channel_id
        status = p.status()
        status.update(
            platform=p.key,
            platform_name=p.name,
            channel=p.channel_path(here) if here is not None else None,
            channel_id=here,
            users=[{"name": u.name, "muted": u.muted, "deaf": u.deaf} for u in p.users_in_channel(here)],
            follow_user=self.follow_user,
        )
        return status

    def channels_json(self) -> list[dict]:
        return [{"id": c.id, "path": c.path, "users": c.users} for c in self.platform.channels()]

    def library_json(self) -> dict:
        return {
            "sounds": [s.to_json() for s in self.library.sounds.values()],
            "categories": self.library.categories,
        }

    def state_json(self) -> dict:
        return {
            "status": self.status_json(),
            "library": self.library_json(),
            "playing": self.playing_json(),
            "volume": self.mixer.volume,
            "activity": [a.to_json() for a in self.activity],
            "prefix": self.cfg.chat_prefix,
            "peer": self._peer_json(),
            "max_upload_mb": self.cfg.max_upload_mb,
            "music": self.music.to_json(),
            "max_clip_seconds": self.cfg.max_clip_seconds,
            "clip_category": self.cfg.clip_category,
            "extras": self.extras_json(),
            "collections": self.catalog.to_json(),
        }

    def _peer_json(self) -> dict | None:
        if not self.cfg.peer_web_url and not self.cfg.peer_web_port:
            return None
        other = "discord" if self.cfg.platform == "mumble" else "mumble"
        url = self.cfg.peer_web_url
        if url and not url.endswith("/"):
            url += "/"
        return {"platform": other, "name": other.capitalize(), "port": self.cfg.peer_web_port, "url": url}

    def publish_status(self) -> None:
        self.events.publish("status", self.status_json())

    def publish_library(self) -> None:
        self.events.publish("library", self.library_json())

    # ---------------------------------------------------- platform events

    async def on_platform_ready(self, *, default_channel: str, server: str) -> None:
        p = self.platform
        follow = self.follow_user
        if follow and p.find_user(follow) is None:
            log.info("Follow-User %s ist nicht online", follow)
        if self._desired_channel is None or not p.has_channel(self._desired_channel):
            # First connect: configured channel. After a reconnect the last chosen channel is kept.
            self._desired_channel = None
            if default_channel:
                channel = p.find_channel(default_channel)
                if channel:
                    self._desired_channel = channel
                else:
                    log.warning("Kanal %r nicht gefunden - bleibe im Standardkanal", default_channel)
        self._move_attempts = 0
        self._reconcile_event.set()
        self.mixer.wake()  # continue the music after a reconnect
        self.log_activity("System", "system", f"Verbunden mit {server}")
        self.publish_status()

    async def on_platform_disconnected(self, reason: str) -> None:
        self.mixer.stop()
        self.log_activity("System", "system", f"Getrennt: {reason}")
        self.publish_status()

    async def on_text(self, msg: IncomingText) -> None:
        # Commands run in the background: resolving a YouTube link takes seconds and must not
        # block the platform's read loop.
        task = asyncio.create_task(self._handle_command(msg))
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)

    async def _handle_command(self, msg: IncomingText) -> None:
        try:
            await self.commands.handle(msg)
        except Exception:  # noqa: BLE001 - a bad command must never kill the connection
            log.exception("Fehler bei Befehl %r von %s", msg.text, msg.sender)

    async def on_user_moved(self, user: PlatformUser, *, old_channel: ChannelId | None, is_me: bool) -> None:
        here = self.platform.channel_id
        if (self.cfg.join_sounds and not is_me and here is not None and user.channel_id == here
                and old_channel != user.channel_id):
            task = asyncio.create_task(self._play_join_sound(user))
            self._command_tasks.add(task)
            task.add_done_callback(self._command_tasks.discard)
        follow = self.follow_user
        if follow and user.name.casefold() == follow.casefold():
            self._reconcile_event.set()
        elif is_me and self._desired_channel is not None and not follow:
            # Moved by someone else (admin drag & drop): accept the new channel as desired.
            if old_channel is not None and old_channel != user.channel_id and user.channel_id != self._last_move[0]:
                self._desired_channel = user.channel_id
        self.publish_status()
