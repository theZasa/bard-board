"""Large, read-only sound collections (e.g. all voice lines of a game).

A collection is a sub folder of the collections folder. Its sub folders are groups (e.g. speakers),
an optional `_index.csv` adds the spoken text, a translation, the situation and the mood. Collections
are searchable and playable, but never part of the normal board: thousands of tiles would make the
board unusable, and the board's polling scan and pre-decoding are built for a few hundred sounds.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .library import AUDIO_EXTENSIONS, Sound, fold

log = logging.getLogger("catalog")

INDEX_FILE = "_index.csv"
# File names that carry no readable text: "1000198965", "AI_DRYBENTE_0HO_FWGQQ_00002_01BENTE_3005"
_CODE_NAME = re.compile(r"^[\dA-Z_\-]+$")
_COLUMNS = {
    # The sub folder the file is in ("Ordner" wins when an index has both)
    "folder": ("ordner", "folder"),
    "group": ("charakter", "sprecher", "held", "gruppe", "character", "speaker", "hero", "group"),
    # Who speaks when the folders are something else, e.g. events in the Dota chat wheel
    "speaker": ("sprecher/team", "team", "caster", "speaker/team"),
    "file": ("datei", "file", "filename"),
    "text": ("text (deutsch)", "text", "text (de)", "text (original)", "zitat", "bezeichnung"),
    "text_en": ("text (englisch)", "text (english)", "text (en)", "english", "übersetzung (englisch)",
                "translation"),
    "situation": ("situation", "kontext", "context", "kategorie"),
    # Lines people know by number, e.g. the Age of Empires taunts ("!aoe 30" is WOLOLO)
    "number": ("nummer", "number", "nr", "taunt"),
    "mood": ("stimmung", "mood"),
}
# Unresolved game localisation keys: "#dota_chatwheel_source_voice_of_god" -> "Voice of God"
_LOC_KEY = re.compile(r"^#\w*?_source_(\w+)$")
_LOC_PREFIX = re.compile(r"^#\w+\s+-\s+")
_MOODS = {"positive": "positiv", "positve": "positiv", "positiv": "positiv", "negative": "negativ", "negativ": "negativ"}
MOODS = ("positiv", "negativ")
# Second and third takes of the same line: "Zeile (2).mp3" -> "Zeile"
_TAKE = re.compile(r"\s+\(\d+\)$")
# Files still being written by an export or download
_PARTIAL = (".part", ".tmp", ".crdownload", ".download")


def _is_audio(name: str) -> bool:
    if name.startswith((".", "~")):
        return False
    stem, dot, ext = name.rpartition(".")
    if not dot or f".{ext.casefold()}" not in AUDIO_EXTENSIONS:
        return False
    return not stem.casefold().endswith(_PARTIAL)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text or "").strip()


def group_label(folder: str) -> str:
    """Folders starting with "_" sort last in the file system, the label drops the underscore."""
    return folder.lstrip("_").strip() or folder


def speaker_label(raw: str) -> str:
    raw = raw.strip()
    m = _LOC_KEY.match(raw)
    if m:
        words = m.group(1).split("_")
        return " ".join(w if w in ("of", "the") and i else w.capitalize() for i, w in enumerate(words))
    return raw.lstrip("#")


def compact(text: str) -> str:
    return fold(text).replace(" ", "")


@dataclass
class Item:
    id: str
    rel: str  # relative to the collection folder, posix separators
    path: Path
    group: str
    text: str
    text_en: str = ""
    situation: str = ""
    mood: str = ""
    speaker: str = ""
    number: str = ""
    folded: str = ""
    folded_text: str = ""
    group_key: str = ""

    @property
    def title(self) -> str:
        return self.text or "Ohne Text"

    @property
    def who(self) -> str:
        """"Anno: Bente" style label: the group, plus the speaker when that is something else."""
        if self.speaker and fold(self.speaker) != self.group_key:
            return f"{self.group} · {self.speaker}"
        return self.group

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "text_en": self.text_en,
            "group": self.group,
            "situation": self.situation,
            "mood": self.mood,
            "speaker": self.speaker,
            "number": self.number,
        }


class Collection:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.name = _nfc(root.name)
        self.slug = compact(self.name) or hashlib.sha1(self.name.encode()).hexdigest()[:8]
        self.commands: tuple[str, ...] = ()
        self.items: dict[str, Item] = {}
        self.groups: list[tuple[str, int]] = []
        self.speakers: list[str] = []
        # Lines in many languages ("Text (Original)"): the web interface shows the translation under the text
        self.mixed_languages = False  # speakers that are not groups themselves (chat wheel casters, teams)
        self.loaded = False
        self.loaded_at = 0.0
        self._index: dict[str, dict] = {}
        self._index_mtime = -1
        self._dir_cache: dict[str, tuple[int, list[Item]]] = {}  # group folder -> (mtime, items)

    # ------------------------------------------------------------------ loading

    def _read_index(self) -> dict[str, dict]:
        """Rows of _index.csv keyed by "<group folder>/<file name>" (both NFC)."""
        path = self.root / INDEX_FILE
        try:
            raw = path.read_text("utf-8-sig")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("%s/%s nicht lesbar: %s", self.name, INDEX_FILE, exc)
            return {}
        first = raw.split("\n", 1)[0]
        delimiter = max(";,\t", key=first.count)
        reader = csv.reader(raw.splitlines(), delimiter=delimiter)
        header = [h.strip().casefold() for h in next(reader, [])]
        cols = {key: next((header.index(n) for n in names if n in header), None) for key, names in _COLUMNS.items()}
        self.mixed_languages = "text (original)" in header
        if cols["file"] is None:
            log.warning("%s/%s hat keine Spalte \"Datei\" - wird ignoriert", self.name, INDEX_FILE)
            return {}

        def cell(row: list[str], key: str) -> str:
            i = cols[key]
            return _nfc(row[i]) if i is not None and i < len(row) else ""

        rows = {}
        for row in reader:
            file = cell(row, "file")
            if file:
                folder = cell(row, "folder") or cell(row, "group")
            rows[f"{folder}/{file}".casefold()] = {k: cell(row, k) for k in _COLUMNS}
        return rows

    def load(self, *, full: bool = False) -> None:
        """(Re)read the folder. Runs in a worker thread and swaps the result in at the end.

        Group folders whose modification time did not change are taken over from the last run, so a
        rescan of a huge collection costs almost nothing while an export keeps adding files (adding or
        renaming a file changes the folder's time). `full` ignores that shortcut."""
        started = time.monotonic()
        index_file = self.root / INDEX_FILE
        try:
            index_mtime = index_file.stat().st_mtime_ns
        except OSError:
            index_mtime = 0
        if full or index_mtime != self._index_mtime:
            self._index = self._read_index() if index_mtime else {}
            self._index_mtime = index_mtime
            self._dir_cache.clear()
        cache: dict[str, tuple[int, list[Item]]] = {}
        items: list[Item] = []
        reused = 0
        with os.scandir(self.root) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                name = _nfc(entry.name)
                if entry.is_dir():
                    if name.startswith("."):
                        continue
                    mtime = entry.stat().st_mtime_ns
                    known = self._dir_cache.get(name)
                    if known is not None and known[0] == mtime:
                        group_items = known[1]
                        reused += 1
                    else:
                        group_items = self._load_group(Path(entry.path), name)
                    cache[name] = (mtime, group_items)
                    items.extend(group_items)
                elif _is_audio(name):
                    items.append(self._make_item(Path(entry.path), name, "", name))
        self._dir_cache = cache

        def order(item: Item) -> tuple:
            number = int(item.number) if item.number.isdigit() else 1 << 30
            return (item.group_key, number, not item.text, item.folded_text, item.rel)

        items.sort(key=order)
        counts: dict[str, int] = {}
        for item in items:
            counts[item.group] = counts.get(item.group, 0) + 1
        unassigned = {group_label(name) for name in cache if name.startswith("_")}
        self.groups = sorted(counts.items(), key=lambda g: (g[0] in unassigned, fold(g[0])))
        group_keys = {fold(g) for g in counts}
        speakers = {fold(i.speaker): i.speaker for i in items if i.speaker and fold(i.speaker) not in group_keys}
        self.speakers = sorted(speakers.values(), key=fold)
        self.items = {i.id: i for i in items}
        self.loaded = True
        self.loaded_at = time.time()
        log.info(
            "Sammlung %s: %d Zeilen (%d mit Text) in %d Gruppen, %d Ordner unverändert, %.1f s",
            self.name, len(items), sum(1 for i in items if i.text), len(self.groups), reused,
            time.monotonic() - started,
        )

    def _load_group(self, folder: Path, folder_name: str) -> list[Item]:
        group = group_label(folder_name)
        group_key = fold(group)
        found = []
        for current, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            sub = os.path.relpath(current, folder)
            prefix = folder_name if sub == "." else f"{folder_name}/{_nfc(sub).replace(os.sep, '/')}"
            for f in files:
                if _is_audio(f):
                    item = self._make_item(Path(current, f), f"{prefix}/{_nfc(f)}", group, folder_name)
                    item.group_key = group_key
                    found.append(item)
        return found

    def _make_item(self, path: Path, rel: str, group: str, group_folder: str) -> Item:
        file_name = rel.rsplit("/", 1)[-1]
        row = self._index.get(f"{group_folder if group else ''}/{file_name}".casefold(), {})
        stem = file_name.rsplit(".", 1)[0]
        speaker = speaker_label(row.get("speaker", ""))
        if not row.get("text") and not row.get("text_en"):  # "Team Liquid - Voiceline 3" -> "Voiceline 3"
            for prefix in filter(None, (row.get("speaker", ""), speaker)):
                stem = stem.removeprefix(f"{prefix} - ")
            stem = _LOC_PREFIX.sub("", stem)
        # Without a text in the main language the English one beats a file name (Dota: "Text (Englisch)" only)
        text = row.get("text") or row.get("text_en") or ("" if _CODE_NAME.match(stem) else _TAKE.sub("", stem))
        text_en = row.get("text_en", "")
        if fold(text_en) == fold(text):
            text_en = ""
        item = Item(
            id=hashlib.sha1(f"{self.name}/{rel}".encode()).hexdigest()[:12],
            rel=rel,
            path=path,
            group=group or self.name,
            text=text,
            text_en=text_en,
            situation=row.get("situation", ""),
            mood=_MOODS.get(row.get("mood", "").casefold(), ""),
            speaker=speaker,
            number=row.get("number", "").lstrip("#"),
        )
        item.folded_text = fold(text) if text else ""
        item.group_key = fold(item.group)
        extra = " ".join(fold(x) for x in (item.text_en, item.situation, item.speaker) if x)
        item.folded = " ".join(filter(None, (item.folded_text, extra, item.group_key)))
        return item

    # ------------------------------------------------------------------ queries

    def get(self, item_id: str) -> Item | None:
        return self.items.get(item_id)

    def matching(self, query: str = "", group: str | list[str] = "", mood: str = "") -> list[Item]:
        """`group` is a group or speaker name, or several of them (any matches)."""
        words = fold(query).split()
        wanted = {g.casefold() for g in ([group] if isinstance(group, str) else group) if g}
        if len(words) == 1 and words[0].isdigit():  # a line's number, if the collection numbers its lines
            numbered = [i for i in self.items.values() if i.number == words[0]
                        and (not wanted or i.group.casefold() in wanted or i.speaker.casefold() in wanted)]
            if numbered:
                return numbered
        hits = [
            i for i in self.items.values()
            if (not wanted or i.group.casefold() in wanted or i.speaker.casefold() in wanted)
            and (not mood or i.mood == mood)
            and all(w in i.folded for w in words)
        ]
        if len(words) > 1:  # the exact phrase in the spoken text first
            phrase = " ".join(words)
            hits.sort(key=lambda i: phrase not in i.folded_text)
        return hits

    def search(self, query: str = "", group: str | list[str] = "", mood: str = "", offset: int = 0, limit: int = 100
               ) -> tuple[int, list[Item]]:
        hits = self.matching(query, group, mood)
        return len(hits), hits[offset:offset + limit]

    def pick(self, query: str = "", group: str | list[str] = "", mood: str = "") -> tuple[Item | None, int]:
        """A random matching line (lines with text preferred) and the number of matches."""
        hits = self.matching(query, group, mood)
        spoken = [i for i in hits if i.text]
        pool = spoken or hits
        return (random.choice(pool) if pool else None), len(hits)

    def example_query(self) -> str:
        """Something to search for that exists: a line number, or a word from a random line."""
        items = list(self.items.values())
        if not items:
            return "@name"
        item = random.choice(items)
        if item.number:
            return item.number
        words = [w for w in re.findall(r"[^\W\d_]{4,}", item.text)]
        return words[0].casefold() if words else f"@{item.group.split()[0].casefold()}"

    def find_groups(self, name: str) -> list[str]:
        """Groups (or speakers) for a (partial) name, best match level first: "bente" -> ["Bente Jorgensen"]."""
        q = fold(name)
        if not q:
            return []
        names = [g for g, _ in self.groups] + self.speakers
        for predicate in (
            lambda g: fold(g) == q,
            lambda g: fold(g).startswith(q),
            lambda g: f" {q}" in f" {fold(g)}",
        ):
            hits = [g for g in names if predicate(g)]
            if hits:
                return hits
        return []

    def as_sound(self, item: Item) -> Sound:
        """A transient Sound for the mixer; it never enters the library."""
        try:
            st = item.path.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        name = item.text if len(item.text) <= 120 else item.text[:117].rsplit(" ", 1)[0] + " …"
        return Sound(
            id=item.id, rel=f"{self.name}/{item.rel}", name=name or f"{item.who}: Ohne Text",
            category=f"{self.name}/{item.group}", path=item.path, size=size, mtime=mtime,
        )

    def board_filename(self, item: Item) -> str:
        """File name for a copy on the board: the spoken text, shortened for the tile."""
        text = item.text or item.path.stem
        text = re.sub(r"\s+", " ", text).strip().rstrip(".")
        if len(text) > 80:
            text = text[:80].rsplit(" ", 1)[0].rstrip(",;:-") + " …"
        return f"{text}{item.path.suffix}"

    def to_json(self) -> dict:
        return {
            "id": self.slug,
            "name": self.name,
            "commands": list(self.commands),
            "loaded": self.loaded,
            "count": len(self.items),
            "groups": [{"name": g, "count": c} for g, c in self.groups],
            "moods": [m for m in MOODS if any(i.mood == m for i in self.items.values())],
            "translations": self.mixed_languages,
        }


class Collections:
    """All collections below one or more root folders; the first root wins when a name exists twice."""

    def __init__(self, roots: Path | list[Path]) -> None:
        self.roots = [roots] if isinstance(roots, Path) else list(roots)
        self.items: dict[str, Collection] = {}
        self.discover()

    def discover(self) -> bool:
        """Look for collection folders (again). Known collections keep what they already read.
        True if collections appeared or disappeared, e.g. after filling an empty volume."""
        found: dict[str, Collection] = {}
        for root in self.roots:
            try:
                paths = sorted(root.iterdir(), key=lambda p: p.name.casefold()) if root.is_dir() else []
            except OSError:
                continue
            for path in paths:
                if not path.is_dir() or path.name.startswith((".", "_")):
                    continue
                c = Collection(path)
                if c.slug in found:
                    if not self.items:  # only worth a log line once, not on every rescan
                        log.info("Sammlung %s gibt es doppelt, verwende %s (nicht %s)",
                                 c.name, found[c.slug].root, path)
                    continue
                known = self.items.get(c.slug)
                found[c.slug] = known if known is not None and known.root == path else c
        changed = {(s, c.root) for s, c in found.items()} != {(s, c.root) for s, c in self.items.items()}
        self.items = dict(sorted(found.items(), key=lambda kv: fold(kv[1].name)))
        return changed

    def assign_commands(self, reserved: set[str]) -> None:
        """Chat commands per collection, never shadowing an existing command."""
        taken = {r.casefold() for r in reserved}
        for c in self.items.values():
            c.commands = ()
        for c in self.items.values():
            # "Anno 1800" answers to /anno and /anno1800, "Dota Chatwheel" (after "Dota 2" took /dota)
            # to /chatwheel and /dotachatwheel
            words = [w for w in fold(c.name).split() if w.isalpha()]
            letters = re.match(r"[^\W\d_]+", c.slug)
            names = []
            for name in (*words, letters.group(0) if letters else "", c.slug):
                if len(name) >= 3 and name not in taken and name not in names:
                    names.append(name)
                    taken.add(name)
            c.commands = tuple(names)

    def __iter__(self):
        return iter(self.items.values())

    def __len__(self) -> int:
        return len(self.items)

    def get(self, slug: str) -> Collection | None:
        return self.items.get(slug)

    def by_command(self, name: str) -> Collection | None:
        name = name.casefold()
        return next((c for c in self.items.values() if name in c.commands), None)

    def to_json(self) -> list[dict]:
        return [c.to_json() for c in self.items.values()]
