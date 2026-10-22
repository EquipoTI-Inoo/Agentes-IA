"""File logging with rotation (Phase 7, B7): logs/jinora.log, 2 MB x 5 files, plus the console."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT_DIR / "logs"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_HANDLER_TAG = "_jinora_file_handler"


def setup_logging(
    log_dir: Optional[Path] = None,
    level: int = logging.INFO,
    max_bytes: int = 2_000_000,
    backup_count: int = 5,
    console: bool = True,
) -> Path:
    """Idempotent: calling it again replaces the Jinora file handler instead of stacking duplicates."""
    directory = Path(log_dir) if log_dir else LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / "jinora.log"

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()

    file_handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    setattr(file_handler, _HANDLER_TAG, True)
    root.addHandler(file_handler)

    if console and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in root.handlers
    ):
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(stream)
    return log_file
