"""Keeps yt-dlp current: YouTube changes regularly break older versions."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger("updater")

PACKAGE = "yt-dlp[default]"
TIMEOUT = 120


def update_ytdlp() -> None:
    """Upgrade yt-dlp inside the running environment before it is imported. Never fatal."""
    if not os.access(sys.prefix, os.W_OK):
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
