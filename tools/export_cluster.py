"""Pack sounds, collections and bot data into one tar for the cluster (run via export.bat).

Runs inside the bot container so it sees exactly what the bot sees: the same collection names (an original
folder "anno1800" mounted as "Anno 1800" ends up as "Anno 1800"), the same ignore rules, UTF-8 names.
The archive is laid out like the mounts of the uploader pod, so `tar -xf - -C /` there puts everything in
place: sounds/, collections/<name>/, data/mumble/, data/discord/.
Certificates, caches and temporary files are left out on purpose.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, "/app")

from app.catalog import Collections  # noqa: E402
from app.config import Config  # noqa: E402

ARCHIVE = "bard-board-daten.tar"
PARTIAL = (".part", ".tmp", ".crdownload", ".download")
DATA_FILES = ("library.json", "joinsounds.json", "aliases.json", "radio.json", "settings.json", "music.json")


class HashingWriter:
    """File wrapper for tarfile's stream mode: hashes and counts while writing."""

    def __init__(self, path: Path) -> None:
        self.file = path.open("wb")
        self.sha = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        self.sha.update(data)
        self.size += len(data)
        return self.file.write(data)

    def close(self) -> None:
        self.file.close()


def _normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 1000
    info.uname = info.gname = ""
    info.mode = 0o755 if info.isdir() else 0o644
    return info


def add_tree(tar: tarfile.TarFile, source: Path, arcname: str, skip_dir) -> tuple[int, int]:
    """Add the files below source (not the folder entry itself, the mount point already exists)."""
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(source):
        rel = Path(dirpath).relative_to(source)
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(rel / d))
        if rel != Path("."):
            tar.add(dirpath, f"{arcname}/{rel.as_posix()}", recursive=False, filter=_normalize)
        for name in sorted(filenames):
            if name.startswith(".") or name.casefold().endswith(PARTIAL):
                continue
            path = Path(dirpath) / name
            tar.add(path, f"{arcname}/{(rel / name).as_posix()}", recursive=False, filter=_normalize)
            files += 1
            size += path.stat().st_size
    return files, size


def mb(n: int) -> str:
    return f"{n / 1e6:,.0f} MB".replace(",", ".")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/export")
    cfg = Config.from_env()
    ignore = {d.casefold() for d in cfg.ignore_dirs}
    started = time.monotonic()
    tmp = out / (ARCHIVE + ".part")
    writer = HashingWriter(tmp)
    contents: list[str] = []
    with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        # Board sounds: the same folders the bot shows (no "_", "." or IGNORE_DIRS folders).
        files, size = add_tree(tar, cfg.sounds_dir, "sounds",
                               lambda rel: rel.name.startswith((".", "_")) or rel.name.casefold() in ignore)
        print(f"Sounds: {files} Dateien, {mb(size)}", flush=True)
        contents.append(f"sounds/ - Board-Sounds ({files} Dateien, {mb(size)})")

        for collection in Collections(list(cfg.collections_dirs)).items.values():
            files, size = add_tree(tar, collection.root, f"collections/{collection.name}",
                                   lambda rel: rel.name.startswith("."))
            print(f"Sammlung {collection.name}: {files} Dateien, {mb(size)}", flush=True)
            contents.append(f"collections/{collection.name}/ ({files} Dateien, {mb(size)})")

        for bot, data_dir in (("mumble", cfg.data_dir), ("discord", Path("/data-discord"))):
            names = [n for n in DATA_FILES if (data_dir / n).is_file()]
            for name in names:
                tar.add(data_dir / name, f"data/{bot}/{name}", filter=_normalize)
            if names:
                print(f"Daten {bot}: {', '.join(names)}", flush=True)
                contents.append(f"data/{bot}/ - {', '.join(names)}")
    writer.close()
    tmp.replace(out / ARCHIVE)
    digest = writer.sha.hexdigest()
    (out / (ARCHIVE + ".sha256")).write_text(f"{digest}  {ARCHIVE}\n", encoding="utf-8")
    (out / "ANLEITUNG.md").write_text(GUIDE.format(size=mb(writer.size), contents="\n".join(f"- {c}" for c in contents)),
                                      encoding="utf-8")
    print(f"Fertig: {ARCHIVE}, {mb(writer.size)} in {time.monotonic() - started:.0f} s", flush=True)
    return 0


GUIDE = """# bard-board: Sounds, Sammlungen und Daten einspielen

`bard-board-daten.tar` ({size}) enthält:

{contents}

Zertifikat und Zugangsdaten sind nicht enthalten, die kommen als Kubernetes-Secrets (siehe DEPLOYMENT.md).
Die Befehle gehen von Release `soundboard` im Namespace `soundboard` aus; das Chart muss schon installiert sein.

## 1. Datei prüfen

```bash
sha256sum -c bard-board-daten.tar.sha256
```

## 2. Bots anhalten und Hilfs-Pod starten

Die Bots schreiben ihre Daten beim Beenden zurück; damit sie die eingespielten Dateien nicht überschreiben, laufen sie währenddessen nicht.

```bash
kubectl -n soundboard scale deploy/soundboard-bard-board-mumble deploy/soundboard-bard-board-discord --replicas=0
helm upgrade soundboard oci://ghcr.io/thezasa/charts/bard-board -n soundboard --reuse-values --version <installierte Version> --set uploader.enabled=true
kubectl -n soundboard rollout status deploy/soundboard-bard-board-dateien
```

## 3. Einspielen (ein Befehl, streamt direkt in die Volumes)

```bash
kubectl -n soundboard exec -i deploy/soundboard-bard-board-dateien -- tar -xf - --no-same-owner --no-overwrite-dir -C / < bard-board-daten.tar
```

Läuft nur ein Bot, meldet tar für den Ordner des anderen unter `data/` einen Fehler; der Rest ist trotzdem eingespielt.

## 4. Hilfs-Pod beenden und Bots starten

```bash
helm upgrade soundboard oci://ghcr.io/thezasa/charts/bard-board -n soundboard --reuse-values --version <installierte Version> --set uploader.enabled=false
kubectl -n soundboard scale deploy/soundboard-bard-board-mumble deploy/soundboard-bard-board-discord --replicas=1
```

Die Sammlungen erscheinen nach dem Start im Webinterface, bei rund 100.000 Dateien dauert das erste Einlesen ein paar Minuten.
"""


if __name__ == "__main__":
    sys.exit(main())
