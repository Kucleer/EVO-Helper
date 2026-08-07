from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from evo_helper.infrastructure.logging import configure_logging


@pytest.fixture(autouse=True)
def restore_logger() -> Iterator[None]:
    """configure_logging mutates a process-global logger.

    Without restoring it, `propagate = False` leaks into later tests and their
    caplog assertions silently stop seeing records.
    """
    logger = logging.getLogger("evo_helper")
    saved = (list(logger.handlers), logger.level, logger.propagate)
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.handlers, logger.level, logger.propagate = saved[0], saved[1], saved[2]


def test_timing_lines_reach_the_log_file(tmp_path: Path) -> None:
    path = configure_logging(log_dir=tmp_path, console=False)

    logging.getLogger("evo_helper.vision.live_reports").info("read attack report in 3.20s")

    assert "read attack report in 3.20s" in path.read_text(encoding="utf-8")


def test_calling_twice_does_not_duplicate_lines(tmp_path: Path) -> None:
    """A second call must replace handlers, not stack them."""
    configure_logging(log_dir=tmp_path, console=False)
    path = configure_logging(log_dir=tmp_path, console=False)

    logging.getLogger("evo_helper.vision.live_reports").info("once")

    assert path.read_text(encoding="utf-8").count("once") == 1


def test_the_log_directory_is_created(tmp_path: Path) -> None:
    path = configure_logging(log_dir=tmp_path / "nested" / "logs", console=False)
    assert path.parent.is_dir()
