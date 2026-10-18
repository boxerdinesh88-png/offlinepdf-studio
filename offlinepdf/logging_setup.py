"""Local-only logging. Rotating files inside the app-owned data folder."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from offlinepdf.config import ensure_app_dirs

__all__ = ["log_file_path", "setup_logging"]

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_configured = False


def log_file_path() -> Path:
    """Path of the active log file."""
    return ensure_app_dirs()["logs"] / "offlinepdf.log"


def setup_logging(*, level: int = logging.INFO, console: bool = True) -> Path:
    """Configure the root logger once: rotating local file + optional console."""
    global _configured
    log_path = log_file_path()
    if _configured:
        return log_path

    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(_FORMAT)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    logging.getLogger("offlinepdf").info("logging to %s", log_path)
    _configured = True
    return log_path
