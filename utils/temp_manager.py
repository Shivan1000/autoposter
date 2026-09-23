"""
utils/temp_manager.py

Per-job temporary directory lifecycle management.

Usage::

    async with JobTempDir(job_id="abc-123") as tmp:
        audio_path = tmp.path / "voiceover.mp3"
        ...
    # On success: directory is deleted.
    # On failure: directory is KEPT and its path is logged for debugging.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

logger = logging.getLogger(__name__)

_TMP_ROOT = Path("tmp")


class JobTempDir:
    """Async context manager that owns a per-job temp directory.

    The directory is created under `tmp/<job_id>/` on entry.
    - On clean exit (`__aexit__` with no exception): directory is removed.
    - On exception: directory is preserved so you can inspect partial artefacts.
    """

    def __init__(self, job_id: Optional[str] = None) -> None:
        self.job_id: str = job_id or uuid.uuid4().hex[:12]
        self.path: Path = _TMP_ROOT / self.job_id
        self._success: bool = False

    # ------------------------------------------------------------------
    # Async context manager protocol
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "JobTempDir":
        self.path.mkdir(parents=True, exist_ok=True)
        logger.debug("Created temp dir: %s", self.path)
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> bool:
        if exc_type is None:
            self._cleanup()
        else:
            logger.warning(
                "Pipeline failed — preserving temp dir for debugging: %s",
                self.path.resolve(),
            )
        # Never suppress exceptions
        return False

    # ------------------------------------------------------------------
    # Sync context manager protocol (for non-async callers / test scripts)
    # ------------------------------------------------------------------

    def __enter__(self) -> "JobTempDir":
        self.path.mkdir(parents=True, exist_ok=True)
        logger.debug("Created temp dir: %s", self.path)
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> bool:
        if exc_type is None:
            self._cleanup()
        else:
            logger.warning(
                "Pipeline failed — preserving temp dir for debugging: %s",
                self.path.resolve(),
            )
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cleanup(self) -> None:
        try:
            shutil.rmtree(self.path)
            logger.debug("Cleaned up temp dir: %s", self.path)
        except OSError as exc:
            logger.warning("Could not remove temp dir %s: %s", self.path, exc)

    def subpath(self, *parts: str) -> Path:
        """Return a path inside this temp dir, creating parent dirs if needed."""
        p = self.path.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
