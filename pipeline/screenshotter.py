"""
pipeline/screenshotter.py

Capture a screenshot of a Reddit thread using Playwright (headless Chromium).

Approach:
  - Navigate to the Reddit thread URL
  - Wait for the post title and comment tree to render
  - Crop to just the title + top N comments (avoids nav bars, sidebars)
  - Save as PNG to the per-job temp directory

The browser runs headless by default. If you need to access a private/NSFW
sub, place a saved Playwright storage state file at `.playwright_session.json`
(exported via `playwright codegen` with an authenticated session).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    async_playwright,
)

from utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

# Path to a saved Playwright auth session (optional)
_SESSION_FILE = Path(".playwright_session.json")

# Selectors — Reddit's new UI (new.reddit.com or www.reddit.com)
# We navigate to old.reddit.com for more predictable layout.
_POST_TITLE_SELECTOR = "h1"
_COMMENT_WRAPPER_SELECTOR = "div[data-testid='comment']"

# Viewport size for the headless browser
_VIEWPORT_WIDTH = 800
_VIEWPORT_HEIGHT = 1200

# How many pixels from the top to start the crop (skip the sticky header)
_CROP_TOP_OFFSET = 0
# How many pixels to include (title + comments, no sidebar)
_CROP_HEIGHT = 900
# Left margin for the crop (skip Reddit sidebar)
_CROP_LEFT = 0
_CROP_WIDTH = 800


class ScreenshotterError(RuntimeError):
    """Raised when the screenshot stage fails after retries."""


class Screenshotter:
    """Capture Reddit thread screenshots via Playwright.

    Args:
        viewport_width: Browser viewport width in pixels.
        viewport_height: Browser viewport height in pixels.
        timeout_ms: Maximum time (ms) to wait for page load.
    """

    def __init__(
        self,
        viewport_width: int = _VIEWPORT_WIDTH,
        viewport_height: int = _VIEWPORT_HEIGHT,
        timeout_ms: int = 30_000,
    ) -> None:
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.timeout_ms = timeout_ms

    async def capture(
        self,
        thread_url: str,
        output_path: Path,
        storage_state: Optional[str] = None,
    ) -> Path:
        """Capture a screenshot of *thread_url* and save it to *output_path*.

        Args:
            thread_url: Full URL to the Reddit thread.
            output_path: Where to write the PNG file.
            storage_state: Path to a Playwright auth state file (optional).

        Returns:
            *output_path* on success.

        Raises:
            ScreenshotterError: On Playwright errors or empty output.
        """
        # Always use old.reddit.com — consistent, no JS-heavy lazy loading
        url = self._to_old_reddit(thread_url)
        logger.info("Capturing screenshot: %s", url)

        try:
            await self._run_capture(url, output_path, storage_state)
        except PlaywrightError as exc:
            raise ScreenshotterError(
                f"Playwright error capturing {url}: {exc}"
            ) from exc

        # Validate output
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise ScreenshotterError(
                f"Screenshot was not created or is empty: {output_path}"
            )

        logger.info(
            "Screenshot saved: %s (%.1f KB)",
            output_path, output_path.stat().st_size / 1024,
        )
        return output_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @retry_with_backoff(
        exceptions=(PlaywrightError, TimeoutError),
        max_attempts=3,
        wait_min=2.0,
    )
    async def _run_capture(
        self,
        url: str,
        output_path: Path,
        storage_state: Optional[str],
    ) -> None:
        async with async_playwright() as p:
            browser: Browser = await p.chromium.launch(headless=True)
            context_kwargs: dict = {
                "viewport": {"width": self.viewport_width, "height": self.viewport_height},
                "color_scheme": "dark",
                "locale": "en-US",
            }

            # Use saved auth session if provided or default file exists
            session_path = storage_state or (
                str(_SESSION_FILE) if _SESSION_FILE.exists() else None
            )
            if session_path:
                logger.debug("Using Playwright session: %s", session_path)
                context_kwargs["storage_state"] = session_path

            context: BrowserContext = await browser.new_context(**context_kwargs)
            page: Page = await context.new_page()

            # Block images, fonts, media — we only need the HTML/CSS
            await page.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,mp4,webm,mp3,woff,woff2,ttf,otf}",
                lambda route: route.abort(),
            )

            try:
                await page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
                await self._wait_for_content(page)
                await self._hide_noise(page)

                output_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(
                    path=str(output_path),
                    clip={
                        "x": _CROP_LEFT,
                        "y": _CROP_TOP_OFFSET,
                        "width": _CROP_WIDTH,
                        "height": _CROP_HEIGHT,
                    },
                    full_page=False,
                )
            finally:
                await context.close()
                await browser.close()

    async def _wait_for_content(self, page: Page) -> None:
        """Wait for the post title to appear on the page."""
        try:
            await page.wait_for_selector(
                ".thing.link, .Post, h1, .title",
                timeout=self.timeout_ms,
            )
            # Extra settle time for CSS
            await page.wait_for_timeout(1500)
        except PlaywrightError:
            logger.warning("Content selector timeout — proceeding with current state")

    async def _hide_noise(self, page: Page) -> None:
        """Hide Reddit UI chrome that shouldn't appear in the screenshot."""
        selectors_to_hide = [
            "#header",
            "#header-bottom-left",
            "#header-bottom-right",
            ".side",
            ".sidebar",
            ".ad-container",
            '[data-testid="frontpage-sidebar"]',
            ".promotedlink",
            "#cookie-consent-banner",
            ".global-announcement",
        ]
        hide_script = """
            (selectors) => {
                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => {
                        el.style.display = 'none';
                    });
                });
            }
        """
        try:
            await page.evaluate(hide_script, selectors_to_hide)
        except PlaywrightError as exc:
            logger.debug("Could not hide some UI elements: %s", exc)

    @staticmethod
    def _to_old_reddit(url: str) -> str:
        """Redirect to old.reddit.com for a predictable layout."""
        return (
            url.replace("https://www.reddit.com", "https://old.reddit.com")
               .replace("https://reddit.com", "https://old.reddit.com")
        )


if __name__ == "__main__":
    import asyncio
    import sys
    from utils.logging_config import setup_logging

    setup_logging()

    async def _test(url: str) -> None:
        out = Path("tmp/test_screenshot.png")
        screenshotter = Screenshotter()
        result = await screenshotter.capture(url, out)
        print(f"Screenshot saved to: {result}")

    test_url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.reddit.com/r/AskReddit/comments/1b3h6mn/"
    )
    asyncio.run(_test(test_url))
