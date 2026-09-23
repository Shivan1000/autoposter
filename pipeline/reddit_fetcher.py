"""
pipeline/reddit_fetcher.py

Fetch trending posts from a subreddit using PRAW (the official Reddit API).

Never scrapes HTML. Respects Reddit API rate limits (PRAW handles this
automatically). Filters stickied posts, NSFW (if configured), and posts
already tracked in the dedup store.

Returns a structured `RedditPost` dataclass ready for downstream stages.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import praw
import prawcore.exceptions
from dotenv import load_dotenv

from pipeline.dedup_store import DedupStore
from utils.retry import retry_with_backoff

load_dotenv()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RedditPost:
    """Structured representation of a Reddit post ready for the pipeline."""
    id: str
    title: str
    permalink: str          # e.g. /r/AskReddit/comments/abc123/...
    url: str                # Full URL to the Reddit thread
    subreddit: str          # subreddit name without r/
    score: int
    top_comments: list[str] = field(default_factory=list)
    is_nsfw: bool = False
    author: Optional[str] = None
    flair: Optional[str] = None


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

class RedditFetcher:
    """Fetch and rank trending posts from Reddit via PRAW.

    Args:
        time_filter: One of "hour", "day", "week", "month", "year", "all".
        candidate_pool: How many top posts to evaluate before picking one.
        allow_nsfw: Whether to include NSFW-tagged posts.
        top_comments_count: Number of top-level comments to capture.
        min_score: Minimum post score to consider.
    """

    def __init__(
        self,
        time_filter: str = "day",
        candidate_pool: int = 10,
        allow_nsfw: bool = False,
        top_comments_count: int = 5,
        min_score: int = 100,
    ) -> None:
        self.time_filter = time_filter
        self.candidate_pool = candidate_pool
        self.allow_nsfw = allow_nsfw
        self.top_comments_count = top_comments_count
        self.min_score = min_score
        self._reddit = self._build_reddit_client()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_post(
        self,
        subreddit: str,
        dedup_store: DedupStore,
    ) -> RedditPost:
        """Fetch the best eligible post from *subreddit*.

        Args:
            subreddit: Subreddit name without r/.
            dedup_store: Used to skip already-posted IDs.

        Returns:
            A :class:`RedditPost` instance.

        Raises:
            ValueError: If no eligible post is found after filtering.
            prawcore.exceptions.PrawcoreException: On Reddit API errors
                (caught and retried by the @retry decorator below).
        """
        logger.info("Fetching top posts from r/%s (time=%s)", subreddit, self.time_filter)

        candidates = self._fetch_candidates(subreddit)
        logger.debug("Retrieved %d raw candidates from r/%s", len(candidates), subreddit)

        for submission in candidates:
            skip_reason = await self._should_skip(submission, dedup_store)
            if skip_reason:
                logger.debug("Skipping %s: %s", submission.id, skip_reason)
                continue

            comments = self._fetch_top_comments(submission)
            post = RedditPost(
                id=submission.id,
                title=submission.title,
                permalink=submission.permalink,
                url=f"https://www.reddit.com{submission.permalink}",
                subreddit=submission.subreddit.display_name,
                score=submission.score,
                top_comments=comments,
                is_nsfw=submission.over_18,
                author=str(submission.author) if submission.author else "[deleted]",
                flair=submission.link_flair_text,
            )
            logger.info(
                "Selected post: [%s] %s (score=%d)",
                post.id, post.title[:60], post.score,
            )
            return post

        raise ValueError(
            f"No eligible posts found in r/{subreddit} with time_filter='{self.time_filter}'. "
            "All candidates were filtered (stickied, NSFW, already posted, or low score)."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @retry_with_backoff(
        exceptions=(prawcore.exceptions.RequestException, prawcore.exceptions.ServerError),
        max_attempts=3,
    )
    def _fetch_candidates(self, subreddit: str) -> list:
        """Pull top-N posts from Reddit. Retried on transient network errors."""
        try:
            sub = self._reddit.subreddit(subreddit)
            return list(sub.top(time_filter=self.time_filter, limit=self.candidate_pool))
        except prawcore.exceptions.Redirect:
            raise ValueError(f"Subreddit r/{subreddit} does not exist or is private.")
        except prawcore.exceptions.Forbidden:
            raise ValueError(f"Subreddit r/{subreddit} is private or quarantined.")
        except prawcore.exceptions.NotFound:
            raise ValueError(f"Subreddit r/{subreddit} not found.")

    async def _should_skip(self, submission, dedup_store: DedupStore) -> Optional[str]:
        """Return a skip reason string, or None if the post is eligible."""
        if submission.stickied:
            return "stickied"
        if submission.score < self.min_score:
            return f"score {submission.score} < min {self.min_score}"
        if submission.over_18 and not self.allow_nsfw:
            return "NSFW"
        if await dedup_store.is_processed(submission.id):
            return "already posted"
        # Skip posts that are external links with no body text (images, links only)
        # — they produce empty scripts. Self-text or video posts preferred.
        if not submission.is_self and submission.selftext == "":
            # Still allow if it has a title long enough to script from
            if len(submission.title.split()) < 5:
                return "link-only post with short title"
        return None

    @retry_with_backoff(
        exceptions=(prawcore.exceptions.RequestException,),
        max_attempts=3,
    )
    def _fetch_top_comments(self, submission) -> list[str]:
        """Fetch and return the top N comment bodies."""
        try:
            submission.comments.replace_more(limit=0)
            comments: list[str] = []
            for comment in submission.comments[:self.top_comments_count]:
                body = comment.body.strip()
                if body and body != "[deleted]" and body != "[removed]":
                    comments.append(body)
            return comments
        except prawcore.exceptions.RequestException as exc:
            logger.warning("Could not fetch comments for %s: %s", submission.id, exc)
            return []

    @staticmethod
    def _build_reddit_client() -> praw.Reddit:
        """Construct a read-only PRAW Reddit client from environment variables."""
        client_id = os.getenv("REDDIT_CLIENT_ID")
        client_secret = os.getenv("REDDIT_CLIENT_SECRET")
        user_agent = os.getenv("REDDIT_USER_AGENT", "autoposter/1.0")

        if not client_id or not client_secret:
            raise EnvironmentError(
                "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set in .env"
            )

        return praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
            ratelimit_seconds=300,  # Wait up to 5 min for rate-limit window reset
        )


if __name__ == "__main__":
    import asyncio
    from utils.logging_config import setup_logging

    setup_logging()

    async def _test() -> None:
        store = DedupStore()
        await store.init()
        fetcher = RedditFetcher()
        post = await fetcher.fetch_post("AskReddit", store)
        print(f"\nFetched post:")
        print(f"  ID:       {post.id}")
        print(f"  Title:    {post.title}")
        print(f"  Score:    {post.score}")
        print(f"  URL:      {post.url}")
        print(f"  Comments: {len(post.top_comments)}")
        if post.top_comments:
            print(f"  Top comment: {post.top_comments[0][:120]}")

    asyncio.run(_test())
