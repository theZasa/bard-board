"""Keeps yt-dlp current: YouTube changes regularly break older versions.

The upgrade itself only happens at startup, before yt-dlp is imported. While the bot runs, it asks PyPI
once a day for a newer release; the soundboard then restarts itself when nothing is playing, and the
startup upgrade installs it. No image rebuild is needed for a new yt-dlp.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
from importlib import metadata

log = logging.getLogger("updater")

PACKAGE = "yt-dlp[default]"
TIMEOUT = 120
PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
CHECK_INTERVAL = 24 * 3600


def can_update() -> bool:
    """pip can only upgrade a writable environment (the image's venv belongs to the app user)."""
    return os.access(sys.prefix, os.W_OK)


def update_ytdlp() -> None:
    """Upgrade yt-dlp inside the running environment before it is imported. Never fatal."""
    if not can_update():
        log.info("yt-dlp-Update übersprungen (Python-Umgebung schreibgeschützt)")
        return
    log.info("Prüfe auf yt-dlp-Updates ...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "--disable-pip-version-check",
             "--no-input", "--timeout", "10", "--retries", "1", PACKAGE],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("yt-dlp-Update fehlgeschlagen (%s), nutze installierte Version", exc)
        return
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        log.warning("yt-dlp-Update fehlgeschlagen (%s), nutze installierte Version", detail[-1] if detail else result.returncode)


def installed_version() -> str | None:
    """Version from the package metadata, i.e. what pip installed (not what is already imported)."""
    try:
        return metadata.version("yt-dlp")
    except metadata.PackageNotFoundError:
        return None


def latest_version(timeout: float = 10) -> str | None:
    """Newest yt-dlp release on PyPI, or None if PyPI cannot be reached."""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=timeout) as response:
            return str(json.load(response)["info"]["version"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.debug("PyPI nicht erreichbar: %s", exc)
        return None


def _parts(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version))


def is_newer(latest: str | None, installed: str | None) -> bool:
    """yt-dlp versions are dates; PyPI writes 2026.8.19 where yt-dlp itself says 2026.08.19."""
    if not latest or not installed:
        return False
    return _parts(latest) > _parts(installed)
