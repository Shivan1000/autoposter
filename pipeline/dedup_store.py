"""
pipeline/dedup_store.py

SQLite-backed store for tracking Reddit posts that have already been
processed (successfully published as Instagram Reels).

A post is ONLY marked as processed after a successful Instagram publish.
Failed pipeline runs leave the post unmarked so it can be retried.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    reddit_id     TEXT PRIMARY KEY,
    subreddit     TEXT NOT NULL,
    reddit_title  TEXT,
    reel_url      TEXT,
    posted_at     TEXT NOT NULL
);
"""


class DedupStore:
    """Async SQLite wrapper for post deduplication.

    Usage::

        store = DedupStore("data/dedup.db")
        await store.init()

        if await store.is_processed("abc123"):
            print("already posted")
        else:
            # ... run pipeline ...
            await store.mark_processed("abc123", subreddit="AskReddit",
                                       title="...", reel_url="https://...")
    """

    def __init__(self, db_path: str | Path = "data/dedup.db") -> None:
        self.db_path = Path(db_path)
        self._db_path_str = str(self.db_path)

    async def init(self) -> None:
        """Create the database file and schema if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path_str) as db:
            await db.execute(_SCHEMA)
            await db.commit()
        logger.debug("DedupStore initialised at %s", self.db_path)

    async def is_processed(self, reddit_id: str) -> bool:
        """Return True if *reddit_id* has been successfully posted before."""
        async with aiosqlite.connect(self._db_path_str) as db:
            async with db.execute(
                "SELECT 1 FROM posts WHERE reddit_id = ?", (reddit_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return row is not None

    async def mark_processed(
        self,
        reddit_id: str,
        subreddit: str,
        title: Optional[str] = None,
        reel_url: Optional[str] = None,
    ) -> None:
        """Record *reddit_id* as successfully published.

        Args:
            reddit_id: The Reddit post's base-36 ID (e.g. "abc123").
            subreddit: The subreddit name (without r/).
            title: Post title, stored for human-readable audit trail.
            reel_url: The published Instagram Reel URL.
        """
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._db_path_str) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO posts
                    (reddit_id, subreddit, reddit_title, reel_url, posted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (reddit_id, subreddit, title, reel_url, now),
            )
            await db.commit()
        logger.info(
            "Marked reddit post %s (%s) as processed → %s",
            reddit_id, subreddit, reel_url or "no URL",
        )

    async def get_recent(self, limit: int = 20) -> list[dict]:
        """Return the *limit* most recently processed posts."""
        async with aiosqlite.connect(self._db_path_str) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM posts ORDER BY posted_at DESC LIMIT ?", (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def count(self) -> int:
        """Return total number of processed posts."""
        async with aiosqlite.connect(self._db_path_str) as db:
            async with db.execute("SELECT COUNT(*) FROM posts") as cursor:
                row = await cursor.fetchone()
        return row[0] if row else 0


if __name__ == "__main__":
    import asyncio

    async def _smoke_test() -> None:
        store = DedupStore("data/test_dedup.db")
        await store.init()

        test_id = "testpost123"
        print(f"Is processed (before): {await store.is_processed(test_id)}")
        await store.mark_processed(test_id, "AskReddit", "Test title", "https://example.com")
        print(f"Is processed (after):  {await store.is_processed(test_id)}")
        print(f"Recent posts: {await store.get_recent(5)}")
        print(f"Total count: {await store.count()}")
        # Cleanup
        Path("data/test_dedup.db").unlink(missing_ok=True)
        print("Smoke test passed ✅")

    asyncio.run(_smoke_test())
