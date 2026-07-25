"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import stat
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import LISTEN_HOST, ConfigurationError, Settings, load_env_file
from .server import CollectorApplication, make_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="allsky-collector",
        description="Normalize local AI-agent OTLP telemetry and forward it to Galileo.",
    )
    parser.add_argument(
        "--env-file",
        metavar="PATH",
        help="load collector variables from a dotenv file; existing environment wins",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration, print a secret-free summary, and exit",
    )
    parser.add_argument(
        "--log-level",
        choices=("debug", "info", "warning", "error"),
        default=os.environ.get("ALLSKY_LOG_LEVEL", "info").lower(),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _warn_env_file_permissions(path: str) -> None:
    env_path = Path(path).expanduser()
    try:
        mode = stat.S_IMODE(env_path.stat().st_mode)
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        logging.getLogger(__name__).warning(
            "env file %s is accessible by group/others; run chmod 600",
            env_path,
        )


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        if args.env_file:
            load_env_file(args.env_file)
            _warn_env_file_permissions(args.env_file)
        settings = Settings.from_environ()
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.check_config:
        print(json.dumps(settings.public_summary(), indent=2, sort_keys=True))
        return 0

    application = CollectorApplication(settings)
    server = make_server(application)
    stopping = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    logging.getLogger(__name__).info(
        "collector listening on %s:%d with content capture %s",
        LISTEN_HOST,
        server.server_address[1],
        "enabled" if settings.capture_content else "disabled",
    )
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return 0


def main() -> None:
    raise SystemExit(run())
