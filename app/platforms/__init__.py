"""Voice platform adapters. One soundboard instance serves exactly one platform."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config
    from .base import VoicePlatform


def make_platform(cfg: "Config") -> "VoicePlatform":
    if cfg.platform == "discord":
        from .discordbot import DiscordPlatform  # discord.py is only imported when needed

        return DiscordPlatform(cfg)
    from .mumble import MumblePlatform

    return MumblePlatform(cfg)
