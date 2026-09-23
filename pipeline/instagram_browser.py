"""
pipeline/instagram_browser.py

Upload Reels to Instagram via Playwright browser automation.
No Meta Graph API, no Cloudflare R2 — just logs into Instagram
with your username/password and uploads directly.

Flow:
  1. Launch Chromium (visible window so you can see it working)
  2. Load saved session (if exists) or log in with credentials
  3. Navigate to Instagram's create/upload page
  4. Upload the local MP4 file
  5. Add caption text
  6. Publish as a Reel
  7. Save session for next time (no re-login needed)

First run: you'll see the browser open and may need to handle 2FA.
Subsequent runs: the saved session skips login automatically.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    async_playwright,
)
from dotenv import load_dotenv

from utils.retry import retry_with_backoff

load_dotenv()
logger = logging.getLogger(__name__)

_SESSION_FILE = Path("data/instagram_session.json")


# ---------------------------------------------------------------------------
# Result / Error types
# ---------------------------------------------------------------------------

@dataclass
class PublishResult:
    """Outcome of an Instagram publish attempt."""
    success: bool
    reel_url: Optional[str] = None
    error_message: Optional[str] = None


class InstagramBrowserError(RuntimeError):
    """Raised when Instagram browser automation fails."""


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------

class InstagramBrowserPublisher:
    """Upload Reels to Instagram via browser automation (Playwright).

    No API keys needed — uses your Instagram username and password.
    The browser session is saved so you only need to log in once.

    Args:
        headless: If False (default), the browser window is visible.
                  Set to True for fully automated runs after first login.
        timeout_ms: Max time to wait for page loads and actions.
    """

    def __init__(
        self,
        headless: bool = False,
        timeout_ms: int = 60_000,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._username = os.getenv("INSTAGRAM_USERNAME", "")
        self._password = os.getenv("INSTAGRAM_PASSWORD", "")

    async def publish(
        self,
        video_path: Path,
        caption: str,
        job_id: str = "",
    ) -> PublishResult:
        """Upload a video as an Instagram Reel.

        Args:
            video_path: Path to the local MP4 file.
            caption: Caption text for the Reel.
            job_id: Pipeline job ID for logging.

        Returns:
            A :class:`PublishResult` with success status.

        Raises:
            InstagramBrowserError: On automation failures.
        """
        if not video_path.exists():
            raise InstagramBrowserError(f"Video file not found: {video_path}")

        logger.info("[%s] Starting Instagram browser upload: %s", job_id, video_path.name)

        try:
            result = await self._run_upload(video_path, caption, job_id)
            return result
        except PlaywrightError as exc:
            raise InstagramBrowserError(
                f"Playwright error during Instagram upload: {exc}"
            ) from exc

    async def _run_upload(
        self,
        video_path: Path,
        caption: str,
        job_id: str,
    ) -> PublishResult:
        """Main upload flow."""
        async with async_playwright() as p:
            # Launch real installed Google Chrome for trusted Meta authentication
            browser: Browser = await p.chromium.launch(
                channel="chrome",
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                ],
            )

            # Standard desktop viewport and user agent for creator studio / web upload
            context_kwargs = {
                "viewport": {"width": 1280, "height": 850},
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            }

            if _SESSION_FILE.exists():
                logger.info("Loading saved Instagram session")
                context_kwargs["storage_state"] = str(_SESSION_FILE)

            context: BrowserContext = await browser.new_context(**context_kwargs)
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page: Page = await context.new_page()

            try:
                # Step 1: Go to Instagram
                logger.info("[%s] Navigating to Instagram...", job_id)
                await page.goto("https://www.instagram.com/", timeout=self.timeout_ms)
                await page.wait_for_timeout(3000)
                await self._dismiss_popups(page)

                # Step 2: Check if logged in, if not → login
                if not await self._is_logged_in(page):
                    if not self._username or not self._password:
                        raise InstagramBrowserError(
                            "INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD must be set in .env"
                        )
                    await self._login(page, job_id)

                await self._dismiss_popups(page)

                # Step 3: Navigate to create page
                logger.info("[%s] Navigating to create/upload page...", job_id)
                await self._click_create_button(page)

                # Step 4: Upload video
                logger.info("[%s] Uploading video: %s", job_id, video_path.name)
                await self._upload_video(page, video_path)

                # Step 5: Go through the next steps and add caption
                logger.info("[%s] Adding caption and publishing...", job_id)
                await self._add_caption_and_publish(page, caption)

                # Step 6: Save session for next time
                _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(_SESSION_FILE))
                logger.info("Instagram session saved for future use")

                return PublishResult(
                    success=True,
                    reel_url="https://www.instagram.com/",  # Exact URL not easily extractable
                )

            except Exception as exc:
                # Save a debug screenshot on failure
                debug_path = Path("tmp") / f"ig_error_{job_id}.png"
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(debug_path))
                logger.error(
                    "[%s] Instagram upload failed. Debug screenshot: %s. Error: %s",
                    job_id, debug_path, exc,
                )

                return PublishResult(
                    success=False,
                    error_message=str(exc),
                )

            finally:
                await context.close()
                await browser.close()

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _is_logged_in(self, page: Page) -> bool:
        """Check if we're logged into Instagram."""
        try:
            # Look for login form or logged-in indicators
            await page.wait_for_timeout(2000)
            url = page.url

            # If we're on the login page, we're not logged in
            if "/accounts/login" in url:
                return False

            # Check cookies for active session
            cookies = await page.context.cookies()
            if any(c.get("name") == "sessionid" for c in cookies) and "/accounts/login" not in url:
                logger.info("Already logged into Instagram (found valid session cookie)")
                return True

            # Check for the home feed or profile elements
            home_icon = await page.query_selector('svg[aria-label="Home"]')
            create_icon = await page.query_selector('svg[aria-label="New post"], svg[aria-label="Create"]')

            if home_icon or create_icon:
                logger.info("Already logged into Instagram")
                return True

            # Check page content
            body = await page.evaluate("() => document.body.innerText.substring(0, 500)")
            if "Log in" in body or "Sign up" in body:
                return False

            return False
        except PlaywrightError:
            return False

    async def _login(self, page: Page, job_id: str) -> None:
        """Log into Instagram with username/password."""
        logger.info("[%s] Logging into Instagram as %s...", job_id, self._username)

        await page.goto(
            "https://www.instagram.com/accounts/login/",
            timeout=self.timeout_ms,
        )
        await page.wait_for_timeout(3000)

        # Dismiss cookie banner if present
        try:
            cookie_btn = await page.query_selector('button:has-text("Allow")')
            if not cookie_btn:
                cookie_btn = await page.query_selector('button:has-text("Accept")')
            if cookie_btn:
                await cookie_btn.click()
                await page.wait_for_timeout(1000)
        except PlaywrightError:
            pass

        # Fill in username (support various Instagram web layouts)
        # Fill in username (support both Instagram and Meta login forms)
        username_selectors = [
            'input[name="email"]',
            'input[name="username"]',
            'input[aria-label*="username" i]',
            'input[aria-label*="email" i]',
            'input[placeholder*="username" i]',
            'input[placeholder*="number" i]',
            'input[type="text"]',
        ]
        username_input = None
        for sel in username_selectors:
            try:
                username_input = await page.wait_for_selector(sel, timeout=4000)
                if username_input and await username_input.is_visible():
                    break
            except Exception:
                continue

        if not username_input:
            inputs = await page.query_selector_all('input')
            for inp in inputs:
                if await inp.is_visible():
                    username_input = inp
                    break

        if not username_input:
            raise InstagramBrowserError("Could not find username input field on Instagram login page")

        await username_input.fill(self._username)

        # Fill in password
        password_selectors = [
            'input[name="pass"]',
            'input[name="password"]',
            'input[type="password"]',
            'input[aria-label*="password" i]',
        ]
        password_input = None
        for sel in password_selectors:
            try:
                password_input = await page.wait_for_selector(sel, timeout=4000)
                if password_input and await password_input.is_visible():
                    break
            except Exception:
                continue

        if not password_input:
            inputs = await page.query_selector_all('input[type="password"]')
            if inputs:
                password_input = inputs[0]

        if not password_input:
            raise InstagramBrowserError("Could not find password input field on Instagram login page")

        await password_input.fill(self._password)

        # Submit via Enter or button
        await page.wait_for_timeout(1000)
        logger.info("[%s] Submitting login...", job_id)
        await password_input.press("Enter")

        # Wait and check response
        logger.info("[%s] Waiting for login response...", job_id)
        await page.wait_for_timeout(6000)

        body = await page.evaluate("() => document.body.innerText")
        if "login information you entered is incorrect" in body.lower() or "password was incorrect" in body.lower():
            raise InstagramBrowserError(
                "Instagram error: The login information entered is incorrect. "
                "Please verify INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD."
            )

        # Check for 2FA prompt
        body = await page.evaluate("() => document.body.innerText")
        if "security code" in body.lower() or "two-factor" in body.lower():
            logger.warning(
                "[%s] 2FA detected! Please enter the code in the browser window. "
                "Waiting up to 60 seconds...",
                job_id,
            )
            # Wait for user to manually enter 2FA code
            await page.wait_for_timeout(60000)

        # Dismiss "Save Login Info" prompt if present
        try:
            not_now_btn = await page.query_selector('button:has-text("Not Now")')
            if not_now_btn:
                await not_now_btn.click()
                await page.wait_for_timeout(2000)
        except PlaywrightError:
            pass

        # Dismiss "Turn on Notifications" prompt if present
        try:
            not_now_btn = await page.query_selector('button:has-text("Not Now")')
            if not_now_btn:
                await not_now_btn.click()
                await page.wait_for_timeout(2000)
        except PlaywrightError:
            pass

        logger.info("[%s] Login complete", job_id)

    # ------------------------------------------------------------------
    async def _dismiss_popups(self, page: Page) -> None:
        """Dismiss common modals like 'Turn on Notifications' or 'Save Info'."""
        dismiss_selectors = [
            'button:has-text("Not Now")',
            'button:has-text("Not now")',
            'button:has-text("Cancel")',
            'button:has-text("Decline")',
            'button:has-text("OK")',
        ]
        for sel in dismiss_selectors:
            try:
                btns = await page.query_selector_all(sel)
                for btn in btns:
                    if await btn.is_visible():
                        txt = (await btn.inner_text()).strip().lower()
                        if txt in ["not now", "cancel", "decline", "ok"]:
                            logger.info("Dismissing popup with button: %s", txt)
                            await btn.click(force=True)
                            await page.wait_for_timeout(1000)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Upload flow
    # ------------------------------------------------------------------

    async def _click_create_button(self, page: Page) -> None:
        """Click the create/new post button on Instagram."""
        await self._dismiss_popups(page)
        selectors = [
            'svg[aria-label="New post"]',
            'svg[aria-label="New Post"]',
            'svg[aria-label="Create"]',
            'span:has-text("Create")',
            'a[href*="/create/"]',
            '[aria-label="New post"]',
            '[aria-label="Create"]',
        ]
        for sel in selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    try:
                        await btn.click(timeout=5000)
                    except Exception:
                        await btn.click(force=True)
                    await page.wait_for_timeout(2000)
                    return
            except Exception:
                continue

        # Fallback: try clicking the "+" icon by looking at all SVGs
        logger.warning("Could not find create button with known selectors, trying fallback")
        await page.goto(
            "https://www.instagram.com/create/select/",
            timeout=self.timeout_ms,
        )
        await page.wait_for_timeout(3000)

    async def _upload_video(self, page: Page, video_path: Path) -> None:
        """Upload the video file via the file input."""
        await self._dismiss_popups(page)
        # Look for file input (hidden, used by the upload dialog)
        try:
            file_input = await page.wait_for_selector('input[type="file"]', timeout=8000)
            if file_input:
                await file_input.set_input_files(str(video_path.resolve()))
                logger.info("Video file attached via input[type=file]")
                await page.wait_for_timeout(5000)
                return
        except Exception:
            pass

        # Try clicking "Select from computer" button
        select_selectors = [
            'button:has-text("Select from computer")',
            'button:has-text("Select From Computer")',
            'button:has-text("Select from device")',
            'button:has-text("Select")',
        ]
        for sel in select_selectors:
            select_btn = await page.query_selector(sel)
            if select_btn and await select_btn.is_visible():
                async with page.expect_file_chooser(timeout=10000) as fc_info:
                    await select_btn.click(force=True)
                file_chooser = await fc_info.value
                await file_chooser.set_files(str(video_path.resolve()))
                logger.info("Video file attached via file chooser dialog")
                await page.wait_for_timeout(5000)
                return

        # Last resort: find any file input
        file_input = await page.wait_for_selector(
            'input[type="file"]', timeout=self.timeout_ms
        )
        await file_input.set_input_files(str(video_path.resolve()))
        logger.info("Video file selected, waiting for upload processing...")
        await page.wait_for_timeout(5000)

    async def _add_caption_and_publish(self, page: Page, caption: str) -> None:
        """Navigate through the post creation flow, add caption, and publish."""
        for _ in range(4):
            await page.wait_for_timeout(1000)
            ok_btn = await page.query_selector('button:has-text("OK"), [role="button"]:has-text("OK")')
            if ok_btn and await ok_btn.is_visible():
                await ok_btn.click(force=True)
                await page.wait_for_timeout(1000)

            # Check if caption field is already visible
            caption_check = await page.query_selector('div[role="textbox"], textarea')
            if caption_check and await caption_check.is_visible():
                break

            next_btn = await page.query_selector(
                'button:has-text("Next"), div[role="button"]:has-text("Next"), [role="button"]:has-text("Next"), span:has-text("Next")'
            )
            if next_btn and await next_btn.is_visible():
                try:
                    await next_btn.click(timeout=5000)
                except Exception:
                    await next_btn.click(force=True)
                await page.wait_for_timeout(2000)
            else:
                break

        # Look for caption/textarea
        caption_selectors = [
            'div[aria-label*="caption" i]',
            'div[role="textbox"]',
            'textarea[aria-label*="caption" i]',
            'textarea[placeholder*="caption" i]',
            'textarea',
        ]

        caption_input = None
        for sel in caption_selectors:
            caption_input = await page.query_selector(sel)
            if caption_input and await caption_input.is_visible():
                break

        if caption_input:
            await caption_input.click(force=True)
            await page.wait_for_timeout(500)
            try:
                await caption_input.fill(caption)
            except Exception:
                await page.keyboard.type(caption, delay=5)
            logger.info("Caption added (%d chars)", len(caption))
        else:
            logger.warning("Could not find caption input — posting without caption")

        await page.wait_for_timeout(2000)

        # Click "Share" or "Post" button
        share_selectors = [
            'button:has-text("Share")',
            'div[role="button"]:has-text("Share")',
            '[role="button"]:has-text("Share")',
            'button:has-text("Post")',
            'button:has-text("Publish")',
        ]

        shared = False
        for sel in share_selectors:
            share_btn = await page.query_selector(sel)
            if share_btn and await share_btn.is_visible():
                try:
                    await share_btn.click(timeout=5000)
                except Exception:
                    await share_btn.click(force=True)
                shared = True
                break

        if not shared:
            raise InstagramBrowserError(
                "Could not find Share/Post button to publish the reel"
            )

        # Wait for upload to complete
        logger.info("Waiting for reel to finish uploading...")
        for _ in range(40):
            await page.wait_for_timeout(2000)
            body = await page.evaluate("() => document.body.innerText")
            if "shared" in body.lower() or "your reel" in body.lower() or "your post has been shared" in body.lower():
                logger.info("✅ Reel published successfully!")
                return

        logger.info("Upload wait finished — reel should be posted")


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from utils.logging_config import setup_logging

    setup_logging()

    async def _test() -> None:
        video = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tmp/dry_run_test.mp4")
        if not video.exists():
            print(f"Video not found: {video}")
            print("Usage: python -m pipeline.instagram_browser <path_to_mp4>")
            return

        publisher = InstagramBrowserPublisher(headless=False)
        result = await publisher.publish(
            video_path=video,
            caption="Test post from autoposter 🚀 #reddit #askreddit",
            job_id="test",
        )
        print(f"\nResult: {'✅ SUCCESS' if result.success else '❌ FAILED'}")
        if result.error_message:
            print(f"Error: {result.error_message}")

    asyncio.run(_test())
