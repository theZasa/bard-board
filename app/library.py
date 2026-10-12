"""Sound library: scans the sounds folder, keeps per-sound metadata and resolves names."""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("library")

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".webm", ".wma", ".mp4"}
# Characters that are not allowed in Windows file names (the sounds folder is often a Windows bind mount).
_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def fold(text: str) -> str:
    """Normalize text for matching: case-insensitive, accents removed, punctuation collapsed."""
    text = text.casefold().replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w]+", " ", text)
    return re.sub(r"[\s_]+", " ", text).strip()


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    name = _INVALID_NAME_CHARS.sub("", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name or name.split(".")[0].casefold() in _RESERVED_WINDOWS:
        raise ValueError("Ungültiger Dateiname")
    return name[:120]


def safe_category(category: str) -> str:
    parts = [safe_filename(p) for p in re.split(r"[\\/]+", category.strip()) if p.strip()]
    return "/".join(parts)


def sound_id(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]


@dataclass
class Sound:
    id: str
    rel: str  # path relative to the sounds folder, posix separators
    name: str
    category: str
    path: Path
    size: int
    mtime: float
    duration: float | None = None
    volume: int = 100
    favorite: bool = False
    plays: int = 0
    last_played: float | None = None
    speaker: str = ""  # who says it (lines taken from a collection)
    text: str = ""  # the full spoken text when the file name had to be shortened
    folded: str = field(default="", repr=False)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "file": self.rel,
            "size": self.size,
            "duration": self.duration,
            "volume": self.volume,
            "favorite": self.favorite,
            "plays": self.plays,
            "last_played": self.last_played,
            "speaker": self.speaker,
            "text": self.text,
        }


@dataclass
class Match:
    sounds: list[Sound]
    exact: bool

    @property
    def unique(self) -> Sound | None:
        return self.sounds[0] if len(self.sounds) == 1 else None


class Library:
    def __init__(self, sounds_dir: Path, meta_file: Path, trash_dir: Path, ignore_dirs: list[str]) -> None:
        self.sounds_dir = sounds_dir
        self.meta_file = meta_file
        self.trash_dir = trash_dir
        self.ignore_dirs = {d.casefold() for d in ignore_dirs}
        self.sounds: dict[str, Sound] = {}
        self._meta: dict[str, dict] = {}
        self._signature: tuple = ()
        self._load_meta()

    # ---------------------------------------------------------------- metadata

    def _load_meta(self) -> None:
        try:
            self._meta = json.loads(self.meta_file.read_text("utf-8"))
        except FileNotFoundError:
            self._meta = {}
        except (OSError, ValueError) as exc:
            log.error("library.json ist beschädigt (%s) - starte mit leeren Metadaten", exc)
            backup = self.meta_file.with_suffix(f".broken-{int(time.time())}.json")
            shutil.copy2(self.meta_file, backup)
            self._meta = {}

    def _sync_meta(self) -> None:
        for s in self.sounds.values():
            entry = {"volume": s.volume, "favorite": s.favorite, "plays": s.plays, "last_played": s.last_played}
            if s.duration is not None:
                entry["duration"] = s.duration
            if s.speaker:
                entry["speaker"] = s.speaker
            if s.text:
                entry["text"] = s.text
            self._meta[s.rel] = entry

    def save_meta(self) -> None:
        self._sync_meta()
        self.meta_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._meta, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
        os.replace(tmp, self.meta_file)

    # ------------------------------------------------------------------ scanning

    def _walk(self) -> list[tuple[str, Path, os.stat_result]]:
        found = []
        self.sounds_dir.mkdir(parents=True, exist_ok=True)
        for root, dirs, files in os.walk(self.sounds_dir):
            dirs[:] = sorted(
                d for d in dirs if not d.startswith(".") and not d.startswith("_") and d.casefold() not in self.ignore_dirs
            )
            for f in files:
                p = Path(root) / f
                if p.suffix.casefold() not in AUDIO_EXTENSIONS or f.startswith("."):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                found.append((p.relative_to(self.sounds_dir).as_posix(), p, st))
        return found

    def scan(self) -> bool:
        """Rescan the folder. Returns True when the set of sounds changed."""
        entries = self._walk()
        signature = tuple(sorted((rel, st.st_size, st.st_mtime_ns) for rel, _, st in entries))
        if signature == self._signature:
            return False
        self._signature = signature
        self._sync_meta()
        sounds: dict[str, Sound] = {}
        previous = {s.rel: s for s in self.sounds.values()}
        for rel, path, st in entries:
            meta = self._meta.get(rel, {})
            old = previous.get(rel)
            category = Path(rel).parent.as_posix()
            sound = Sound(
                id=sound_id(rel),
                rel=rel,
                name=unicodedata.normalize("NFC", path.stem),
                category="" if category == "." else category,
                path=path,
                size=st.st_size,
                mtime=st.st_mtime,
                duration=old.duration if old and old.mtime == st.st_mtime else meta.get("duration"),
                volume=int(meta.get("volume", 100)),
                favorite=bool(meta.get("favorite", False)),
                plays=int(meta.get("plays", 0)),
                last_played=meta.get("last_played"),
                speaker=str(meta.get("speaker", "")),
                text=str(meta.get("text", "")),
            )
            sound.folded = fold(sound.name)
            sounds[sound.id] = sound
        self.sounds = dict(sorted(sounds.items(), key=lambda kv: (kv[1].category.casefold(), kv[1].name.casefold())))
        log.info("%d Sounds in %s gefunden", len(self.sounds), self.sounds_dir)
        return True

    # ------------------------------------------------------------------- queries

    @property
    def categories(self) -> list[str]:
        return sorted({s.category for s in self.sounds.values()}, key=str.casefold)

    def get(self, sid: str) -> Sound | None:
        return self.sounds.get(sid)

    def resolve(self, query: str) -> Match:
        """Find sounds for a chat query. Prefers exact > prefix > word > substring > fuzzy matches."""
        q = fold(query)
        if not q:
            return Match([], False)
        if "/" in query:  # "kategorie/name"
            cat, _, name = query.rpartition("/")
            fc, fn = fold(cat), fold(name)
            hits = [s for s in self.sounds.values() if fold(s.category) == fc and s.folded == fn]
            if hits:
                return Match(hits, True)
        all_sounds = list(self.sounds.values())
        exact = [s for s in all_sounds if s.folded == q]
        if exact:
            return Match(exact, True)
        for predicate in (
            lambda s: s.folded.startswith(q),
            lambda s: f" {q}" in f" {s.folded}",
            lambda s: q in s.folded,
            lambda s: q.replace(" ", "") in s.folded.replace(" ", ""),
        ):
            hits = [s for s in all_sounds if predicate(s)]
            if hits:
                return Match(sorted(hits, key=lambda s: (len(s.folded), s.folded)), False)
        names = {s.folded: s for s in all_sounds}
        close = difflib.get_close_matches(q, list(names), n=5, cutoff=0.72)
        return Match([names[c] for c in close], False)

    def search(self, query: str, limit: int = 50) -> list[Sound]:
        q = fold(query)
        if not q:
            return list(self.sounds.values())[:limit]
        words = q.split()
        hits = [s for s in self.sounds.values()
                if all(w in f"{s.folded} {fold(s.category)} {fold(s.speaker)}" for w in words)]
        return hits[:limit]

    # ----------------------------------------------------------------- mutations

    def record_play(self, sound: Sound) -> None:
        sound.plays += 1
        sound.last_played = time.time()

    def _unique_target(self, directory: Path, filename: str) -> Path:
        target = directory / filename
        stem, suffix = Path(filename).stem, Path(filename).suffix
        n = 2
        while target.exists():
            target = directory / f"{stem} ({n}){suffix}"
            n += 1
        return target

    def add_file(self, filename: str, data_path: Path, category: str = "") -> Path:
        """Move an uploaded temp file into the library, returns the final path."""
        name = safe_filename(filename)
        if Path(name).suffix.casefold() not in AUDIO_EXTENSIONS:
            raise ValueError(f"Dateityp {Path(name).suffix or '?'} wird nicht unterstützt")
        directory = self.sounds_dir / safe_category(category) if category else self.sounds_dir
        directory.mkdir(parents=True, exist_ok=True)
        target = self._unique_target(directory, name)
        shutil.move(str(data_path), target)
        return target

    def rename(self, sound: Sound, new_name: str | None = None, new_category: str | None = None) -> Sound:
        name = safe_filename(new_name) if new_name is not None else sound.name
        category = safe_category(new_category) if new_category is not None else sound.category
        directory = self.sounds_dir / category if category else self.sounds_dir
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{name}{sound.path.suffix}"
        if directory / filename == sound.path:
            return sound
        target = self._unique_target(directory, filename)
        shutil.move(str(sound.path), target)
        new_rel = target.relative_to(self.sounds_dir).as_posix()
        self._sync_meta()
        self._meta[new_rel] = self._meta.pop(sound.rel, {})
        self.sounds.pop(sound.id, None)
        self._remove_empty_dirs(sound.path.parent)
        self.scan()
        self.save_meta()
        return self.sounds[sound_id(new_rel)]

    def delete(self, sound: Sound) -> Path:
        """Move the file into the trash folder (nothing is ever deleted for good)."""
        target_dir = self.trash_dir / time.strftime("%Y-%m-%d")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = self._unique_target(target_dir, sound.path.name)
        shutil.move(str(sound.path), target)
        self._sync_meta()
        self._meta.pop(sound.rel, None)
        self.sounds.pop(sound.id, None)
        self._remove_empty_dirs(sound.path.parent)
        self.scan()
        self.save_meta()
        return target

    def _remove_empty_dirs(self, directory: Path) -> None:
        while directory != self.sounds_dir and self.sounds_dir in directory.parents:
            try:
                directory.rmdir()
            except OSError:
                return
            directory = directory.parent
