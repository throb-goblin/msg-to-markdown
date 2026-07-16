"""Logging setup for the MSG to Markdown application."""

from __future__ import annotations

import logging
import queue
import sys
from datetime import datetime
from pathlib import Path


LOGGER_NAME = "msg_to_md"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class QueueLogHandler(logging.Handler):
    """Forward formatted log records into a Tkinter-safe queue."""

    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        super().__init__()
        self._log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._log_queue.put(self.format(record))
        except Exception:
            self.handleError(record)


def setup_logging(log_dir: Path) -> tuple[logging.Logger, Path]:
    """Configure file and console logging and return the application logger."""

    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"msg_to_md_{timestamp}.log"

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if _has_console_stream():
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    logger.info("Application start")
    logger.info("Log file: %s", log_path)
    return logger, log_path


def _has_console_stream() -> bool:
    """Return whether this process has a usable console stream."""

    stream = getattr(sys, "stdout", None)
    return stream is not None and hasattr(stream, "write")


def add_queue_handler(
    logger: logging.Logger,
    log_queue: "queue.Queue[str]",
) -> QueueLogHandler:
    """Attach a queue handler used by the GUI live log window."""

    handler = QueueLogHandler(log_queue)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    logger.addHandler(handler)
    return handler
