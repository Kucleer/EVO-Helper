"""Log configuration for the capture and ingestion tools.

Timing lines are the point: they are what tells you a report read went from two
seconds to thirty. They go to a file as well as the console so a long capture
run leaves a record behind rather than scrolling away.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_DIR = Path("var/logs")
DEFAULT_LOG_NAME = "report-read.log"

#: Keep a bounded history; a capture run should never fill the disk.
MAX_BYTES = 2_000_000
BACKUP_COUNT = 5

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(
    *, log_dir: Path | None = None, level: int = logging.INFO, console: bool = True
) -> Path:
    """Send ``evo_helper`` logs to a rotating file, and optionally the console.

    Returns the log file path. Safe to call twice: handlers are replaced rather
    than stacked, so a second call does not duplicate every line.
    """
    directory = log_dir or DEFAULT_LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / DEFAULT_LOG_NAME

    logger = logging.getLogger("evo_helper")
    logger.setLevel(level)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT)
    file_handler = RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    # The root logger has its own handlers in some hosts; do not double-emit.
    logger.propagate = False
    return path
