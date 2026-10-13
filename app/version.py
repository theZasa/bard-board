"""Which build is running. Commit and commit date are baked into the image at build time (CI and
start.bat pass them as build arguments), so the web interface and /help show what is live."""

from __future__ import annotations

import os
import re


def describe() -> str:
    """E.g. "Version 703a78c vom 13.10.2026"; a "+" marks a local build with uncommitted changes."""
    revision = os.environ.get("BARD_BOARD_REVISION", "").strip()
    date = os.environ.get("BARD_BOARD_DATE", "").strip()
    if not revision:
        return "Version unbekannt"
    match = re.fullmatch(r"([0-9a-f]{7,40})(\+?)", revision)
    if match:
        revision = match.group(1)[:7] + match.group(2)
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date)
    return f"Version {revision} vom {match.group(3)}.{match.group(2)}.{match.group(1)}" if match else f"Version {revision}"
