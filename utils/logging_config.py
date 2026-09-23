"""
utils/logging_config.py

Centralised logging setup for autoposter.
Call `setup_logging()` once at startup (main.py / discord_bot.py).
Every module should use `logging.getLogger(__name__)`.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path


def setup_logging(
    log_dir: str = "logs",
    log_filename: str = "autoposter.log",
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
    level_str: str | None = None,
) -> None:
    """Configure root logger with a rotating file handler and a console handler.

    Args:
        log_dir: Directory to write log files into (created if absent).
        log_filename: Name of the rotating log file.
        max_bytes: Maximum size of a single log file before rotation.
        backup_count: How many rotated files to keep.
        level_str: Override log level (DEBUG/INFO/WARNING/ERROR).
                   Falls back to the LOG_LEVEL env var, then INFO.
    """
    level_str = level_str or os.getenv("LOG_LEVEL", "INFO")
    level = getattr(logging, level_str.upper(), logging.INFO)

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # ---- Rotating file handler ----
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_path / log_filename,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    # ---- Console (stream) handler ----
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    # Avoid adding duplicate handlers if called more than once
    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(console_handler)
    else:
        root.handlers.clear()
        root.addHandler(file_handler)
        root.addHandler(console_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)


class JobLoggerAdapter(logging.LoggerAdapter):
    """Wraps a logger to automatically inject `job_id` into every log record.

    Usage::

        logger = JobLoggerAdapter(logging.getLogger(__name__), job_id="abc-123")
        logger.info("Fetching Reddit post")
        # → [INFO] [pipeline.reddit_fetcher] [job:abc-123] Fetching Reddit post
    """

    def __init__(self, logger: logging.Logger, job_id: str) -> None:
        super().__init__(logger, {"job_id": job_id})

    def process(
        self, msg: str, kwargs: dict
    ) -> tuple[str, dict]:
        return f"[job:{self.extra['job_id']}] {msg}", kwargs
