"""
utils/retry.py

Retry decorator factory built on top of `tenacity`.
Every external I/O call in the pipeline should use `@retry_with_backoff(...)`.
"""

from __future__ import annotations

import logging
from typing import Callable, Sequence, Type

from tenacity import (
    RetryError,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# Re-export so callers can catch it without importing tenacity directly.
__all__ = ["retry_with_backoff", "RetryError"]


def retry_with_backoff(
    exceptions: Sequence[Type[BaseException]] = (Exception,),
    max_attempts: int = 3,
    wait_min: float = 1.0,
    wait_max: float = 30.0,
    multiplier: float = 2.0,
    reraise: bool = True,
) -> Callable:
    """Return a tenacity `@retry` decorator with exponential back-off.

    Args:
        exceptions: Tuple of exception types that should trigger a retry.
                    Anything NOT in this list propagates immediately.
        max_attempts: Maximum total attempts (including the first one).
        wait_min: Minimum wait between retries (seconds).
        wait_max: Maximum wait between retries (seconds).
        multiplier: Exponential back-off multiplier.
        reraise: If True, re-raise the last exception after all attempts
                 are exhausted (instead of raising `tenacity.RetryError`).

    Example::

        @retry_with_backoff(
            exceptions=(prawcore.exceptions.RequestException, TimeoutError),
            max_attempts=4,
        )
        def fetch_posts(subreddit: str) -> list[dict]:
            ...
    """
    return retry(
        retry=retry_if_exception_type(tuple(exceptions)),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=multiplier, min=wait_min, max=wait_max),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=reraise,
    )
