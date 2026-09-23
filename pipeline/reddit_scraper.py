"""
pipeline/reddit_scraper.py

Fetch trending posts from Reddit using the public RSS feed (no API keys needed),
then generate a styled Reddit-like screenshot using Playwright.

Approach:
  - Fetch the RSS feed from reddit.com/r/{subreddit}/top.rss?t={time_filter}
  - Parse the Atom XML to extract post titles, links, authors
  - Filter NSFW and already-posted entries (via dedup store)
  - Generate a dark-mode Reddit-style screenshot card via Playwright HTML render
  - Return a RedditPost dataclass + screenshot path

No Reddit API keys required — uses the public RSS feed.
Reddit blocks headless browsers, so screenshots are rendered from HTML templates.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Optional

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    async_playwright,
)

from pipeline.dedup_store import DedupStore
from utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

# Viewport size for the headless browser
_VIEWPORT_WIDTH = 800
_VIEWPORT_HEIGHT = 900

# Atom namespace
_ATOM_NS = "http://www.w3.org/2005/Atom"


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
# Errors
# ---------------------------------------------------------------------------

class RedditScraperError(RuntimeError):
    """Raised when scraping fails after retries."""


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

class RedditScraper:
    """Scrape trending Reddit posts via RSS and generate screenshots.

    No API keys required. Uses Reddit's public RSS feed for post data
    and Playwright to render styled HTML screenshots.

    Args:
        time_filter: One of "hour", "day", "week", "month", "year", "all".
        candidate_pool: How many top posts to evaluate before picking one.
        allow_nsfw: Whether to include NSFW-tagged posts.
        top_comments_count: Number of top-level comments to capture.
        min_score: Minimum post score to consider (RSS scores may be 0).
    """

    def __init__(
        self,
        time_filter: str = "day",
        candidate_pool: int = 10,
        allow_nsfw: bool = False,
        top_comments_count: int = 5,
        min_score: int = 0,
    ) -> None:
        self.time_filter = time_filter
        self.candidate_pool = candidate_pool
        self.allow_nsfw = allow_nsfw
        self.top_comments_count = top_comments_count
        self.min_score = min_score

    async def fetch_and_screenshot(
        self,
        subreddit: str,
        screenshot_path: Path,
        dedup_store: DedupStore,
    ) -> RedditPost:
        """Fetch the best post from *subreddit* and generate a screenshot.

        Args:
            subreddit: Subreddit name without r/.
            screenshot_path: Where to save the screenshot PNG.
            dedup_store: Used to skip already-posted IDs.

        Returns:
            A :class:`RedditPost` with screenshot saved to disk.

        Raises:
            ValueError: If no eligible post is found.
            RedditScraperError: On network errors.
        """
        logger.info("Fetching posts from r/%s via RSS (time=%s)", subreddit, self.time_filter)

        # Step 1: Fetch and parse RSS feed
        candidates = await self._fetch_rss(subreddit)
        logger.info("Found %d candidates from RSS feed", len(candidates))

        if not candidates:
            raise ValueError(
                f"Could not find any posts on r/{subreddit} via RSS. "
                "The subreddit may not exist or the feed may be unavailable."
            )

        # Step 2: Find eligible post
        selected = None
        for candidate in candidates[:self.candidate_pool]:
            skip_reason = await self._should_skip(candidate, dedup_store)
            if skip_reason:
                logger.debug("Skipping %s: %s", candidate["id"], skip_reason)
                continue
            selected = candidate
            break

        if selected is None:
            raise ValueError(
                f"No eligible posts found in r/{subreddit} with time_filter='{self.time_filter}'. "
                "All candidates were filtered (already posted, or title too short)."
            )

        logger.info(
            "Selected post: [%s] %s",
            selected["id"], selected["title"][:60],
        )

        # Step 3 & 4: Fetch real comments and generate styled card screenshot
        comments = await self._extract_comments_and_screenshot(
            selected, subreddit, screenshot_path
        )

        # Validate screenshot
        if not screenshot_path.exists() or screenshot_path.stat().st_size == 0:
            raise RedditScraperError(
                f"Screenshot was not created or is empty: {screenshot_path}"
            )

        logger.info(
            "Screenshot saved: %s (%.1f KB)",
            screenshot_path, screenshot_path.stat().st_size / 1024,
        )

        return RedditPost(
            id=selected["id"],
            title=selected["title"],
            permalink=selected["permalink"],
            url=selected["url"],
            subreddit=subreddit,
            score=selected["score"],
            top_comments=[c["text"] for c in comments],
            is_nsfw=selected.get("is_nsfw", False),
            author=selected.get("author", "[unknown]"),
            flair=selected.get("flair"),
        )

    # ------------------------------------------------------------------
    # RSS Feed fetching
    # ------------------------------------------------------------------

    @retry_with_backoff(
        exceptions=(httpx.HTTPError, httpx.TimeoutException),
        max_attempts=3,
        wait_min=2.0,
    )
    async def _fetch_rss(self, subreddit: str) -> list[dict]:
        """Fetch and parse the public Reddit RSS feed for a subreddit."""
        url = f"https://www.reddit.com/r/{subreddit}/top.rss?t={self.time_filter}&limit=25"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }

        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, headers=headers, timeout=15.0)
            resp.raise_for_status()

        # Parse Atom XML
        root = ET.fromstring(resp.text)
        entries = root.findall(f"{{{_ATOM_NS}}}entry")

        posts = []
        for entry in entries:
            title_el = entry.find(f"{{{_ATOM_NS}}}title")
            link_el = entry.find(f"{{{_ATOM_NS}}}link")
            author_el = entry.find(f"{{{_ATOM_NS}}}author/{{{_ATOM_NS}}}name")
            category_el = entry.find(f"{{{_ATOM_NS}}}category")
            content_el = entry.find(f"{{{_ATOM_NS}}}content")

            if title_el is None or link_el is None:
                continue

            title = unescape(title_el.text or "").strip()
            link = link_el.get("href", "")

            # Extract Reddit post ID from the URL
            post_id = self._extract_post_id(link)
            if not post_id:
                continue

            permalink = link.replace("https://www.reddit.com", "")

            # Try to extract score from the content HTML
            score = 0
            if content_el is not None and content_el.text:
                score = self._extract_score_from_content(content_el.text)

            # Check for NSFW in category
            is_nsfw = False
            if category_el is not None:
                cat_label = category_el.get("label", "").lower()
                if "nsfw" in cat_label:
                    is_nsfw = True

            author = ""
            if author_el is not None and author_el.text:
                author = author_el.text.strip().replace("/u/", "")

            posts.append({
                "id": post_id,
                "title": title,
                "permalink": permalink,
                "url": link,
                "score": score,
                "is_nsfw": is_nsfw,
                "author": author,
                "flair": None,
            })

        return posts

    # ------------------------------------------------------------------
    # Comment fetching & Screenshot generation
    # ------------------------------------------------------------------

    async def _extract_comments_and_screenshot(
        self,
        selected: dict,
        subreddit: str,
        screenshot_path: Path,
    ) -> list[dict]:
        """Fetch comments and render the post card in a single browser session."""
        comments: list[dict] = []
        async with async_playwright() as p:
            browser: Browser = await p.chromium.launch(headless=True)
            context: BrowserContext = await browser.new_context(
                viewport={"width": 800, "height": 1400},
                device_scale_factor=2,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                color_scheme="dark",
            )
            page: Page = await context.new_page()

            try:
                # 1. Fetch live top comments from Reddit thread
                post_url = selected.get("url")
                if post_url:
                    try:
                        resp = await page.goto(post_url, wait_until="domcontentloaded", timeout=12000)
                        if resp and resp.status == 200:
                            try:
                                await page.wait_for_selector("shreddit-comment", timeout=6000)
                            except Exception:
                                await page.wait_for_timeout(2000)

                            comment_els = await page.query_selector_all("shreddit-comment")
                            for el in comment_els:
                                author = await el.get_attribute("author") or "[anonymous]"
                                if author.lower() in ("automoderator", "[deleted]"):
                                    continue
                                body_el = await el.query_selector("div[slot='comment'], p")
                                text = ""
                                if body_el:
                                    text = (await body_el.inner_text()).strip()
                                else:
                                    text = (await el.inner_text()).strip()
                                if len(text) > 8:
                                    clean_text = text.replace("\n", " ").strip()
                                    display_text = clean_text if len(clean_text) <= 160 else clean_text[:157] + "..."
                                    comments.append({"author": author, "text": display_text})
                                if len(comments) >= self.top_comments_count:
                                    break
                    except Exception as exc:
                        logger.warning("Could not extract live comments from %s: %s", post_url, exc)

                selected["comments"] = comments
                logger.info("Extracted %d comments for post [%s]", len(comments), selected["id"])

                # 2. Render styled card HTML with comments
                html = self._build_post_html(selected, subreddit)
                await page.set_content(html)
                await page.wait_for_timeout(350)

                # 3. Screenshot ONLY the .card element (transparent background)
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                card = await page.query_selector(".card")
                if card:
                    await card.screenshot(path=str(screenshot_path), omit_background=True)
                else:
                    await page.screenshot(path=str(screenshot_path))

                logger.info("Generated screenshot with comments for: %s", selected["title"][:60])
                return comments
            finally:
                await context.close()
                await browser.close()

    @staticmethod
    def _build_post_html(selected: dict, subreddit: str) -> str:
        """Build a Reddit-style post card as HTML with top comments."""
        import html as html_mod
        title = html_mod.escape(selected["title"])
        author = html_mod.escape(selected.get("author", "[unknown]"))
        score_text = f"{selected['score']:,}" if selected["score"] > 0 else "Vote"
        comments_list = selected.get("comments", [])

        # Build comments HTML
        comments_html = ""
        if comments_list:
            comment_items = []
            avatar_colors = ["#ff4500", "#0079d3", "#46d160", "#7193ff", "#ffb000", "#9b51e0"]
            for i, c in enumerate(comments_list[:2]):  # show top 1-2 comments
                c_author = html_mod.escape(c["author"])
                c_text = html_mod.escape(c["text"])
                color = avatar_colors[i % len(avatar_colors)]
                initial = (c_author[0].upper() if c_author else "U")
                badge_html = '<span class="comment-badge">TOP COMMENT</span>' if i == 0 else ''
                upvotes = f"{1420 - i * 380:,}"
                comment_items.append(f"""
                <div class="comment">
                    <div class="comment-header">
                        <div class="user-avatar" style="background: {color};">{initial}</div>
                        <span class="comment-author">u/{c_author}</span>
                        {badge_html}
                        <span class="comment-time">• {i + 2}h ago</span>
                    </div>
                    <div class="comment-body">{c_text}</div>
                    <div class="comment-footer">
                        <span class="comment-upvote">⬆ {upvotes} ⬇</span>
                        <span class="comment-reply">Reply</span>
                    </div>
                </div>
                """)
            comments_html = f"""
            <div class="comments-section">
                {''.join(comment_items)}
            </div>
            """

        comments_count_display = f"{len(comments_list)} " if comments_list else ""
        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: transparent;
            color: #d7dadc;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                         'Helvetica Neue', Arial, sans-serif;
            padding: 0;
            margin: 0;
            display: inline-block;
        }}
        .card {{
            background: #1a1a1b;
            border: 1px solid #343536;
            border-radius: 18px;
            padding: 24px 22px;
            width: 720px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.65);
        }}
        .header {{
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 14px;
        }}
        .subreddit-icon {{
            width: 38px;
            height: 38px;
            background: linear-gradient(135deg, #ff4500, #ff6b35);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 15px;
            color: white;
            font-weight: 800;
            flex-shrink: 0;
        }}
        .header-text {{
            display: flex;
            flex-direction: column;
            gap: 2px;
        }}
        .subreddit-name {{
            font-size: 14px;
            font-weight: 700;
            color: #f2f4f5;
        }}
        .post-meta {{
            font-size: 12px;
            color: #818384;
        }}
        .title {{
            font-size: 22px;
            font-weight: 600;
            line-height: 1.35;
            margin-bottom: 16px;
            color: #f2f4f5;
            letter-spacing: -0.2px;
        }}
        .actions {{
            display: flex;
            gap: 10px;
            align-items: center;
            padding-bottom: 14px;
            border-bottom: 1px solid #2d2d2e;
        }}
        .action-btn {{
            display: flex;
            align-items: center;
            gap: 6px;
            background: #272729;
            border-radius: 20px;
            padding: 6px 14px;
            font-size: 12px;
            color: #818384;
            font-weight: 600;
        }}
        .upvote {{ color: #ff4500; font-weight: bold; }}
        .comments-section {{
            margin-top: 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }}
        .comment {{
            background: #212123;
            border-left: 3px solid #ff4500;
            border-radius: 10px;
            padding: 12px 14px;
        }}
        .comment-header {{
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 6px;
        }}
        .user-avatar {{
            width: 22px;
            height: 22px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            font-weight: bold;
            color: white;
            flex-shrink: 0;
        }}
        .comment-author {{
            font-size: 13px;
            font-weight: 700;
            color: #d7dadc;
        }}
        .comment-badge {{
            background: rgba(255, 69, 0, 0.2);
            color: #ff6b35;
            font-size: 10px;
            font-weight: 700;
            padding: 2px 6px;
            border-radius: 4px;
            letter-spacing: 0.5px;
        }}
        .comment-time {{
            font-size: 11px;
            color: #818384;
        }}
        .comment-body {{
            font-size: 14px;
            line-height: 1.4;
            color: #d7dadc;
            margin-bottom: 8px;
        }}
        .comment-footer {{
            display: flex;
            gap: 12px;
            font-size: 11px;
            color: #818384;
            font-weight: 600;
        }}
        .comment-upvote {{
            color: #ff4500;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <div class="subreddit-icon">r/</div>
            <div class="header-text">
                <span class="subreddit-name">r/{subreddit}</span>
                <span class="post-meta">Posted by u/{author} • 6h ago</span>
            </div>
        </div>
        <div class="title">{title}</div>
        <div class="actions">
            <div class="action-btn">
                <span class="upvote">⬆</span>
                <span>{score_text}</span>
                <span>⬇</span>
            </div>
            <div class="action-btn">💬 {comments_count_display}Comments</div>
            <div class="action-btn">↗ Share</div>
        </div>
        {comments_html}
    </div>
</body>
</html>"""

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    async def _should_skip(self, candidate: dict, dedup_store: DedupStore) -> Optional[str]:
        """Return a skip reason string, or None if the post is eligible."""
        if candidate.get("is_nsfw") and not self.allow_nsfw:
            return "NSFW"
        if await dedup_store.is_processed(candidate["id"]):
            return "already posted"
        if len(candidate["title"].split()) < 5:
            return "title too short for scripting"
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_post_id(url: str) -> Optional[str]:
        """Extract the Reddit post ID from a URL."""
        match = re.search(r"/comments/([a-z0-9]+)", url, re.IGNORECASE)
        return match.group(1) if match else None

    @staticmethod
    def _extract_score_from_content(html_content: str) -> int:
        """Try to extract the post score from the RSS content HTML."""
        match = re.search(r"(\d+)\s*(?:points?|score)", html_content, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return 0


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    import sys
    from utils.logging_config import setup_logging

    setup_logging()

    async def _test(sub: str) -> None:
        store = DedupStore("data/dedup.db")
        await store.init()

        scraper = RedditScraper(min_score=0)
        out = Path("tmp/test_screenshot.png")

        post = await scraper.fetch_and_screenshot(sub, out, store)
        print(f"\nFetched post:")
        print(f"  ID:       {post.id}")
        print(f"  Title:    {post.title}")
        print(f"  Score:    {post.score}")
        print(f"  URL:      {post.url}")
        print(f"  Author:   {post.author}")
        print(f"\nScreenshot saved to: {out}")

    test_sub = sys.argv[1] if len(sys.argv) > 1 else "AskReddit"
    asyncio.run(_test(test_sub))
