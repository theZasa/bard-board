"""Persistent per-user join sounds and chat aliases (short commands)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger("extras")

ALIAS_NAME = re.compile(r"^[\w\-]{1,24}$", re.UNICODE)


class _JsonStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict] = {}
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                self.entries = {k: v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.error("%s ist beschädigt (%s) - starte leer", path.name, exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
        os.replace(tmp, self.path)


class JoinSounds(_JsonStore):
    """Mumble user name -> sound (path relative to the sounds folder)."""

    def get(self, user: str) -> str | None:
        entry = self.entries.get(user.casefold())
        return entry["sound"] if entry else None

    def set(self, user: str, sound_rel: str, set_by: str) -> None:
        self.entries[user.casefold()] = {"user": user, "sound": sound_rel, "set_by": set_by, "ts": time.time()}
        self.save()

    def remove(self, user: str) -> bool:
        removed = self.entries.pop(user.casefold(), None) is not None
        if removed:
            self.save()
        return removed

    def sound_moved(self, old_rel: str, new_rel: str | None) -> None:
        """Keep entries in sync when a sound is renamed (new_rel) or deleted (None)."""
        changed = False
        for key, entry in list(self.entries.items()):
            if entry.get("sound") == old_rel:
                if new_rel is None:
                    del self.entries[key]
                else:
                    entry["sound"] = new_rel
                changed = True
        if changed:
            self.save()


class RadioStations(_JsonStore):
    """Saved livestreams ("Sender"): name -> link, played with /radio name."""

    def get(self, name: str) -> dict | None:
        return self.entries.get(name.casefold())

    def set(self, name: str, url: str, title: str, created_by: str) -> None:
        self.entries[name.casefold()] = {"name": name.casefold(), "url": url, "title": title,
                                         "created_by": created_by, "ts": time.time()}
        self.save()

    def remove(self, name: str) -> bool:
        removed = self.entries.pop(name.casefold(), None) is not None
        if removed:
            self.save()
        return removed

    @staticmethod
    def validate_name(name: str) -> str:
        if not ALIAS_NAME.match(name):
            raise ValueError("Sendernamen dürfen nur Buchstaben, Ziffern, - und _ enthalten (max. 24 Zeichen)")
        if name.casefold() in ("add", "del", "rm", "remove", "list", "stop"):
            raise ValueError(f"\"{name}\" ist als Sendername reserviert")
        return name.casefold()


class Aliases(_JsonStore):
    """Short commands: name -> target. Targets are a sound name or a full command like "/play ..."."""

    def get(self, name: str) -> dict | None:
        return self.entries.get(name.casefold())

    def set(self, name: str, target: str, created_by: str) -> None:
        self.entries[name.casefold()] = {"name": name.casefold(), "target": target.strip(), "created_by": created_by,
                                         "ts": time.time()}
        self.save()

    def remove(self, name: str) -> bool:
        removed = self.entries.pop(name.casefold(), None) is not None
        if removed:
            self.save()
        return removed

    @staticmethod
    def validate_name(name: str, reserved: set[str]) -> str:
        if not ALIAS_NAME.match(name):
            raise ValueError("Kürzel dürfen nur Buchstaben, Ziffern, - und _ enthalten (max. 24 Zeichen)")
        if name.casefold() in reserved:
            raise ValueError(f"\"{name}\" ist schon ein Befehl")
        return name.casefold()
