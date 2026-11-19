"""Logging configuration: everything goes to logs/swing.log; warnings also to the console."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import PROJECT_ROOT

_CONFIGURED = False


def setup_logging(log_dir: Path | None = None, console_level: int = logging.WARNING) -> Path:
    """Configure root logging once. Returns the log file path."""
    global _CONFIGURED
    log_dir = log_dir or PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "swing.log"
    if _CONFIGURED:
        return log_file

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    fh = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    ch.setLevel(console_level)
    root.addHandler(ch)

    # yfinance is chatty; keep its noise out of the console.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("peewee").setLevel(logging.WARNING)
    _CONFIGURED = True
    return log_file
