"""Logging: console + rotating file logs, plus a structured event log.

Log files land in LOG_DIR (mounted outside the container via docker-compose):
  bot.log     — application log (errors, warnings, debug)
  events.log  — one line per business event (joins, downloads, payments, ...)
"""

import logging
import os
from logging.handlers import RotatingFileHandler

_FMT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def setup_logging(log_dir: str, level: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter(_FMT)

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    app_file = RotatingFileHandler(
        os.path.join(log_dir, "bot.log"), maxBytes=5_000_000, backupCount=5,
        encoding="utf-8",
    )
    app_file.setFormatter(fmt)
    root.addHandler(app_file)

    events = logging.getLogger("events")
    events.setLevel(logging.INFO)
    events.propagate = False  # events go to their own file only
    ev_file = RotatingFileHandler(
        os.path.join(log_dir, "events.log"), maxBytes=5_000_000, backupCount=5,
        encoding="utf-8",
    )
    ev_file.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    events.addHandler(ev_file)

    logging.getLogger("httpx").setLevel(logging.WARNING)


def event(kind: str, **fields) -> None:
    """Append one structured line to events.log, e.g.:
    2026-08-01 10:00:00 download_request user=123 platform=youtube"""
    payload = " ".join(f"{k}={v}" for k, v in fields.items())
    logging.getLogger("events").info("%s %s", kind, payload)
