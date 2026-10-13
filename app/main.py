"""Entry point: python -m app.main"""

from __future__ import annotations

import faulthandler
import logging
import os
import signal
import sys

import uvicorn

from . import version
from .certs import ensure_certificate
from .config import Config, ConfigError
from .core import Soundboard
from .media import Resolver
from .opus import version as opus_version
from .updater import update_ytdlp
from .web import create_app


def main() -> int:
    # Print a Python traceback if native code (libopus via ctypes) ever crashes the process.
    faulthandler.enable()
    if hasattr(signal, "SIGUSR1"):
        # `docker kill -s USR1 bard-board-mumble` prints the stack of every thread into the log (debugging hangs).
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("main")
    log.info("bard-board, %s, für %s (%s)", version.describe(), cfg.platform.capitalize(), opus_version())
    if cfg.ytdlp_auto_update:
        update_ytdlp()
    log.info("yt-dlp %s", Resolver.version())

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    if cfg.platform == "discord":
        try:
            import discord  # noqa: F401
        except ImportError:
            log.error("discord.py ist nicht installiert - bitte das Image neu bauen (start.bat)")
            return 2
    if cfg.platform == "mumble":
        ensure_certificate(cfg.cert_file, cfg.key_file, cfg.mumble_user)

    board = Soundboard(cfg)
    app = create_app(board)
    log.info("Webinterface lauscht auf %s:%s%s", cfg.web_host, cfg.web_port,
             " (mit Passwort)" if cfg.web_password else "")
    server = uvicorn.Server(uvicorn.Config(
        app,
        host=cfg.web_host,
        port=cfg.web_port,
        log_level="warning",
        ws="websockets-sansio",
        proxy_headers=False,
        server_header=False,
    ))

    def shutdown() -> None:
        server.should_exit = True

    board.shutdown_hook = shutdown
    server.run()
    if board.restart_requested:
        # Replace this process instead of relying on Docker/Kubernetes to restart it: same PID, no
        # restart counter, and it also works without a container. The startup upgrade loads the new yt-dlp.
        log.info("Starte neu, um das neue yt-dlp zu laden ...")
        logging.shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, "-m", "app.main"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
