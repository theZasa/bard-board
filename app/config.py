"""Configuration loaded from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} muss eine Ganzzahl sein (ist: {raw!r})") from exc
    if lo is not None and value < lo or hi is not None and value > hi:
        raise ConfigError(f"{name} muss zwischen {lo} und {hi} liegen (ist: {value})")
    return value


def _float(name: str, default: float, lo: float = 0.0) -> float:
    raw = _str(name)
    if not raw:
        return default
    try:
        value = float(raw.replace(",", "."))
    except ValueError as exc:
        raise ConfigError(f"{name} muss eine Zahl sein (ist: {raw!r})") from exc
    if value < lo:
        raise ConfigError(f"{name} darf nicht kleiner als {lo} sein")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "ja", "on"}:
        return True
    if raw in {"0", "false", "no", "nein", "off"}:
        return False
    raise ConfigError(f"{name} muss true oder false sein (ist: {raw!r})")


def _list(name: str, default: str = "") -> list[str]:
    raw = _str(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    # Which voice platform this instance serves: "mumble" or "discord" (one per container)
    platform: str = "mumble"

    # Mumble connection
    mumble_host: str = ""
    mumble_port: int = 64738
    mumble_user: str = "Soundboard"
    mumble_password: str = field(default="", repr=False)  # secrets never show up in logs or tracebacks
    mumble_channel: str = ""
    mumble_follow_user: str = ""
    mumble_tokens: list[str] = field(default_factory=list, repr=False)
    # Client certificate: by default generated into the data folder; Kubernetes mounts it from a Secret
    mumble_cert_file: Path | None = None
    mumble_key_file: Path | None = None
    mumble_comment: str = ""

    # Discord connection (token comes from the credentials file outside the project folder)
    discord_token: str = field(default="", repr=False)
    discord_guild: str = ""  # server name or id, empty: the only server the bot is in
    discord_channel: str = ""  # voice channel to join on start
    discord_follow_user: str = ""
    discord_text_channels: list[str] = field(default_factory=list)  # extra channels that accept !commands
    discord_admin_role: str = ""

    # Chat commands
    command_prefixes: list[str] = field(default_factory=lambda: ["/", "!"])
    admins: list[str] = field(default_factory=list)
    blocked_users: list[str] = field(default_factory=list)
    command_cooldown: float = 1.0

    # Audio
    max_voices: int = 4
    overlap: bool = True
    default_volume: int = 70
    bitrate: int = 64000
    normalize: bool = True
    trim_silence: bool = True
    max_sound_seconds: int = 300

    # Music queue (YouTube & co)
    music_default_volume: int = 50
    music_duck_volume: int = 25
    music_normalize: bool = True
    music_announce: bool = True
    max_track_minutes: int = 20
    max_playlist_items: int = 100
    max_clip_seconds: int = 60
    clip_category: str = "YouTube Clips"
    ytdlp_auto_update: bool = True

    # Join sounds, text-to-speech, pull timer, aliases
    join_sounds: bool = True
    join_sound_cooldown: int = 600
    tts_model: Path = Path("/opt/piper/voice.onnx")
    tts_max_chars: int = 300
    pull_sound: str = ""
    pull_max_seconds: int = 120

    live_idle_minutes: float = 5.0

    # Files
    sounds_dir: Path = Path("/sounds")
    # Several roots, the first one wins for equal names: the extra mount (an original folder the user
    # keeps filling, e.g. a running export) before the collections folder inside the project.
    collections_dirs: tuple[Path, ...] = (Path("/collections-extern"), Path("/collections"))
    data_dir: Path = Path("/data")
    ignore_dirs: list[str] = field(default_factory=lambda: ["aussortiert"])

    # Web
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    # The other platform's web interface for the Mumble <-> Discord switch: a full URL (server, behind an
    # ingress) or a port on the same host (PC), empty/0 = no switch
    peer_web_url: str = ""
    peer_web_port: int = 0
    # Password for the web interface (HTTP Basic auth, any user name); empty = no protection (local use)
    web_password: str = field(default="", repr=False)
    max_upload_mb: int = 50

    log_level: str = "INFO"

    @property
    def cert_file(self) -> Path:
        return self.mumble_cert_file or self.data_dir / "cert.pem"

    @property
    def key_file(self) -> Path:
        return self.mumble_key_file or self.data_dir / "key.pem"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "papierkorb"

    @property
    def music_file(self) -> Path:
        return self.data_dir / "music.json"

    @property
    def ytdlp_cache_dir(self) -> Path:
        return self.data_dir / "yt-dlp"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def library_file(self) -> Path:
        return self.data_dir / "library.json"

    @property
    def joinsounds_file(self) -> Path:
        return self.data_dir / "joinsounds.json"

    @property
    def radio_file(self) -> Path:
        return self.data_dir / "radio.json"

    @property
    def aliases_file(self) -> Path:
        return self.data_dir / "aliases.json"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    def is_admin(self, user: str) -> bool:
        """Without configured admins everybody may use admin commands."""
        if not self.admins:
            return True
        return user.casefold() in {a.casefold() for a in self.admins}

    def is_blocked(self, user: str) -> bool:
        return user.casefold() in {b.casefold() for b in self.blocked_users}

    @property
    def follow_user_default(self) -> str:
        return self.discord_follow_user if self.platform == "discord" else self.mumble_follow_user

    @property
    def chat_prefix(self) -> str:
        """Prefix shown in hints. In Discord "/" opens the slash command menu, so "!" reads better."""
        if self.platform == "discord":
            return next((p for p in self.command_prefixes if p != "/"), self.command_prefixes[0])
        return self.command_prefixes[0]

    @property
    def default_channel(self) -> str:
        return self.discord_channel if self.platform == "discord" else self.mumble_channel

    @classmethod
    def from_env(cls) -> "Config":
        platform = _str("PLATFORM", "mumble").casefold() or "mumble"
        if platform not in ("mumble", "discord"):
            raise ConfigError("PLATFORM muss mumble oder discord sein")
        host = _str("MUMBLE_HOST")
        if platform == "mumble" and not host:
            raise ConfigError("MUMBLE_HOST fehlt - bitte in der .env eintragen")
        token = _str("DISCORD_TOKEN")
        if platform == "discord" and not token:
            raise ConfigError("DISCORD_TOKEN fehlt - bitte mit zugangsdaten.bat eintragen (Discord)")
        prefixes = _list("COMMAND_PREFIXES", "/,!")
        if not prefixes:
            raise ConfigError("COMMAND_PREFIXES darf nicht leer sein")
        level = _str("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ConfigError("LOG_LEVEL muss DEBUG, INFO, WARNING oder ERROR sein")
        return cls(
            platform=platform,
            discord_token=token,
            discord_guild=_str("DISCORD_GUILD"),
            discord_channel=_str("DISCORD_CHANNEL"),
            discord_follow_user=_str("DISCORD_FOLLOW_USER"),
            discord_text_channels=_list("DISCORD_TEXT_CHANNELS"),
            discord_admin_role=_str("DISCORD_ADMIN_ROLE"),
            mumble_host=host,
            mumble_port=_int("MUMBLE_PORT", 64738, 1, 65535),
            mumble_user=_str("MUMBLE_USER", "Soundboard") or "Soundboard",
            mumble_password=_str("MUMBLE_PASSWORD"),
            mumble_channel=_str("MUMBLE_CHANNEL"),
            mumble_follow_user=_str("MUMBLE_FOLLOW_USER"),
            mumble_tokens=_list("MUMBLE_TOKENS"),
            mumble_comment=_str("MUMBLE_COMMENT"),
            command_prefixes=prefixes,
            admins=_list("ADMINS"),
            blocked_users=_list("BLOCKED_USERS"),
            command_cooldown=_float("COMMAND_COOLDOWN", 1.0),
            max_voices=_int("MAX_VOICES", 4, 1, 16),
            overlap=_bool("OVERLAP", True),
            default_volume=_int("DEFAULT_VOLUME", 70, 0, 100),
            bitrate=_int("BITRATE", 64000, 8000, 510000),
            normalize=_bool("NORMALIZE", True),
            trim_silence=_bool("TRIM_SILENCE", True),
            max_sound_seconds=_int("MAX_SOUND_SECONDS", 300, 1, 3600),
            music_default_volume=_int("MUSIC_DEFAULT_VOLUME", 50, 0, 100),
            music_duck_volume=_int("MUSIC_DUCK_VOLUME", 25, 0, 100),
            music_normalize=_bool("MUSIC_NORMALIZE", True),
            music_announce=_bool("MUSIC_ANNOUNCE", True),
            max_track_minutes=_int("MAX_TRACK_MINUTES", 20, 1, 24 * 60),
            max_playlist_items=_int("MAX_PLAYLIST_ITEMS", 100, 1, 1000),
            max_clip_seconds=_int("MAX_CLIP_SECONDS", 60, 1, 600),
            clip_category=_str("CLIP_CATEGORY", "YouTube Clips") or "YouTube Clips",
            ytdlp_auto_update=_bool("YTDLP_AUTO_UPDATE", True),
            join_sounds=_bool("JOIN_SOUNDS", True),
            join_sound_cooldown=_int("JOIN_SOUND_COOLDOWN", 600, 0, 24 * 3600),
            tts_model=Path(_str("TTS_MODEL", "/opt/piper/voice.onnx")),
            tts_max_chars=_int("TTS_MAX_CHARS", 300, 10, 2000),
            pull_sound=_str("PULL_SOUND"),
            pull_max_seconds=_int("PULL_MAX_SECONDS", 120, 5, 600),
            sounds_dir=Path(_str("SOUNDS_DIR", "/sounds")),
            live_idle_minutes=_float("LIVE_IDLE_MINUTES", 5.0),
            collections_dirs=tuple(Path(p) for p in _list("COLLECTIONS_DIRS", "/collections-extern,/collections")),
            data_dir=Path(_str("DATA_DIR", "/data")),
            ignore_dirs=_list("IGNORE_DIRS", "aussortiert"),
            web_host=_str("WEB_HOST", "0.0.0.0"),
            web_port=_int("WEB_PORT", 8080, 1, 65535),
            peer_web_url=_str("PEER_WEB_URL"),
            peer_web_port=_int("PEER_WEB_PORT", 0, 0, 65535),
            web_password=_str("WEB_PASSWORD"),
            mumble_cert_file=Path(_str("MUMBLE_CERT_FILE")) if _str("MUMBLE_CERT_FILE") else None,
            mumble_key_file=Path(_str("MUMBLE_KEY_FILE")) if _str("MUMBLE_KEY_FILE") else None,
            max_upload_mb=_int("MAX_UPLOAD_MB", 50, 1, 1024),
            log_level=level,
        )
