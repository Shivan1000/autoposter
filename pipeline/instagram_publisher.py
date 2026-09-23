"""
pipeline/instagram_publisher.py

Upload a Reel to Instagram via the official Meta Graph API.

Flow (per Meta documentation):
  1. Upload the final MP4 to a temporary public HTTPS host (Cloudflare R2 or S3)
  2. POST /v19.0/{ig_user_id}/media  → create a container, get container_id
  3. Poll GET /v19.0/{container_id}?fields=status_code until FINISHED or ERROR
  4. POST /v19.0/{ig_user_id}/media_publish  → publish the container
  5. Retrieve the published Reel URL
  6. Optionally delete the temp video from the hosting bucket

References:
  https://developers.facebook.com/docs/instagram-api/reference/ig-user/media
  https://developers.facebook.com/docs/instagram-api/reference/ig-media
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

from utils.retry import retry_with_backoff

load_dotenv()
logger = logging.getLogger(__name__)

_GRAPH_API_BASE = "https://graph.facebook.com/v19.0"
_DEFAULT_POLL_INTERVAL = 5       # seconds between status checks
_DEFAULT_POLL_TIMEOUT = 300      # max seconds to wait for container FINISHED


class InstagramPublisherError(RuntimeError):
    """Raised on Instagram Graph API errors."""


class MetaAPIError(InstagramPublisherError):
    """Raised when the Meta Graph API returns a 4xx/5xx error."""

    def __init__(self, code: int, message: str, fb_error_code: Optional[int] = None) -> None:
        self.http_code = code
        self.fb_error_code = fb_error_code
        super().__init__(
            f"Meta Graph API error {code}"
            + (f" (fb_code={fb_error_code})" if fb_error_code else "")
            + f": {message}"
        )


# ---------------------------------------------------------------------------
# Uploader abstraction
# ---------------------------------------------------------------------------

class VideoUploader(ABC):
    """Abstract interface for temporary public video hosting."""

    @abstractmethod
    async def upload(self, video_path: Path, key: str) -> str:
        """Upload *video_path* and return its public HTTPS URL."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete the uploaded file from the host."""


class R2Uploader(VideoUploader):
    """Upload videos to Cloudflare R2 (S3-compatible)."""

    def __init__(self) -> None:
        import boto3
        account_id = os.environ["R2_ACCOUNT_ID"]
        self._bucket = os.environ["R2_BUCKET_NAME"]
        self._public_base = os.environ["R2_PUBLIC_BASE_URL"].rstrip("/")
        self._s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )

    async def upload(self, video_path: Path, key: str) -> str:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._s3.upload_file(
                str(video_path),
                self._bucket,
                key,
                ExtraArgs={"ContentType": "video/mp4"},
            ),
        )
        url = f"{self._public_base}/{key}"
        logger.info("Uploaded video to R2: %s", url)
        return url

    async def delete(self, key: str) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._s3.delete_object(Bucket=self._bucket, Key=key),
        )
        logger.info("Deleted R2 object: %s", key)


class S3Uploader(VideoUploader):
    """Upload videos to AWS S3."""

    def __init__(self) -> None:
        import boto3
        self._bucket = os.environ["AWS_S3_BUCKET"]
        region = os.environ.get("AWS_S3_REGION", "us-east-1")
        self._public_base = f"https://{self._bucket}.s3.{region}.amazonaws.com"
        self._s3 = boto3.client("s3", region_name=region)

    async def upload(self, video_path: Path, key: str) -> str:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._s3.upload_file(
                str(video_path),
                self._bucket,
                key,
                ExtraArgs={"ContentType": "video/mp4", "ACL": "public-read"},
            ),
        )
        url = f"{self._public_base}/{key}"
        logger.info("Uploaded video to S3: %s", url)
        return url

    async def delete(self, key: str) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._s3.delete_object(Bucket=self._bucket, Key=key),
        )
        logger.info("Deleted S3 object: %s", key)


def build_uploader() -> VideoUploader:
    """Instantiate the correct uploader based on available environment variables."""
    if os.getenv("R2_ACCOUNT_ID"):
        logger.debug("Using Cloudflare R2 uploader")
        return R2Uploader()
    if os.getenv("AWS_S3_BUCKET"):
        logger.debug("Using AWS S3 uploader")
        return S3Uploader()
    raise EnvironmentError(
        "No video hosting credentials found. "
        "Set R2_ACCOUNT_ID (for Cloudflare R2) or AWS_S3_BUCKET (for AWS S3) in .env"
    )


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------

@dataclass
class PublishResult:
    """Result of a successful Instagram publish."""
    reel_url: str           # Full Instagram Reel URL
    media_id: str           # Meta media ID
    video_host_key: str     # Key used on temp host (for cleanup)
    video_host_url: str     # Public URL that was passed to Graph API


class InstagramPublisher:
    """Publish a Reel to Instagram via the Meta Graph API.

    Args:
        poll_interval_sec: How often to poll container status.
        poll_timeout_sec: Max seconds to wait for container to finish processing.
        cleanup_after_publish: Whether to delete the temp hosted video after publish.
    """

    def __init__(
        self,
        poll_interval_sec: int = _DEFAULT_POLL_INTERVAL,
        poll_timeout_sec: int = _DEFAULT_POLL_TIMEOUT,
        cleanup_after_publish: bool = True,
    ) -> None:
        self.poll_interval_sec = poll_interval_sec
        self.poll_timeout_sec = poll_timeout_sec
        self.cleanup_after_publish = cleanup_after_publish
        self._user_id = self._require_env("INSTAGRAM_USER_ID")
        self._access_token = self._require_env("INSTAGRAM_ACCESS_TOKEN")
        self._uploader = build_uploader()

    async def publish(
        self,
        video_path: Path,
        caption: str,
        job_id: str,
    ) -> PublishResult:
        """Upload and publish *video_path* as an Instagram Reel.

        Args:
            video_path: Final composed MP4 file.
            caption: Full caption string (including hashtags).
            job_id: Unique job identifier used as the storage key.

        Returns:
            A :class:`PublishResult` on success.

        Raises:
            MetaAPIError: On Graph API 4xx/5xx errors.
            InstagramPublisherError: On timeout or unexpected failures.
        """
        storage_key = f"autoposter/{job_id}/reel.mp4"

        logger.info("Uploading video to temporary host (key=%s)", storage_key)
        video_url = await self._uploader.upload(video_path, storage_key)

        try:
            container_id = await self._create_container(video_url, caption)
            await self._wait_for_container(container_id)
            media_id = await self._publish_container(container_id)
            reel_url = f"https://www.instagram.com/reel/{media_id}/"

            logger.info("Published Reel: %s (media_id=%s)", reel_url, media_id)

            if self.cleanup_after_publish:
                try:
                    await self._uploader.delete(storage_key)
                except Exception as exc:
                    logger.warning("Could not delete temp video %s: %s", storage_key, exc)

            return PublishResult(
                reel_url=reel_url,
                media_id=media_id,
                video_host_key=storage_key,
                video_host_url=video_url,
            )

        except Exception:
            logger.warning(
                "Publish failed — temp video NOT deleted from host: %s", video_url
            )
            raise

    # ------------------------------------------------------------------
    # Graph API calls
    # ------------------------------------------------------------------

    @retry_with_backoff(
        exceptions=(httpx.NetworkError, httpx.TimeoutException),
        max_attempts=3,
    )
    async def _create_container(self, video_url: str, caption: str) -> str:
        """Step 1: Create a media container. Returns container_id."""
        endpoint = f"{_GRAPH_API_BASE}/{self._user_id}/media"
        payload = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": self._access_token,
        }
        logger.debug("Creating IG media container: %s", endpoint)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(endpoint, data=payload)

        data = self._handle_response(resp, "create container")
        container_id = data.get("id")
        if not container_id:
            raise InstagramPublisherError(
                f"No container ID in Graph API response: {data}"
            )
        logger.info("Container created: %s", container_id)
        return container_id

    async def _wait_for_container(self, container_id: str) -> None:
        """Step 2: Poll container status until FINISHED or timeout."""
        elapsed = 0
        endpoint = f"{_GRAPH_API_BASE}/{container_id}"
        params = {
            "fields": "status_code,status",
            "access_token": self._access_token,
        }

        logger.info("Polling container %s for processing status…", container_id)

        while elapsed < self.poll_timeout_sec:
            await asyncio.sleep(self.poll_interval_sec)
            elapsed += self.poll_interval_sec

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(endpoint, params=params)

            data = self._handle_response(resp, "poll container status")
            status_code = data.get("status_code", "")
            logger.debug(
                "Container %s status: %s (elapsed %ds)", container_id, status_code, elapsed
            )

            if status_code == "FINISHED":
                logger.info("Container %s is ready to publish", container_id)
                return
            if status_code == "ERROR":
                raise InstagramPublisherError(
                    f"Container {container_id} entered ERROR state. "
                    f"Meta status detail: {data}"
                )
            # INPROGRESS / IN_PROGRESS — keep polling

        raise InstagramPublisherError(
            f"Container {container_id} did not finish within {self.poll_timeout_sec}s. "
            "Video may be too large or in an unsupported format."
        )

    @retry_with_backoff(
        exceptions=(httpx.NetworkError, httpx.TimeoutException),
        max_attempts=3,
    )
    async def _publish_container(self, container_id: str) -> str:
        """Step 3: Publish the container. Returns the published media_id."""
        endpoint = f"{_GRAPH_API_BASE}/{self._user_id}/media_publish"
        payload = {
            "creation_id": container_id,
            "access_token": self._access_token,
        }
        logger.info("Publishing container %s", container_id)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(endpoint, data=payload)

        data = self._handle_response(resp, "publish container")
        media_id = data.get("id")
        if not media_id:
            raise InstagramPublisherError(
                f"No media ID in publish response: {data}"
            )
        return media_id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _handle_response(resp: httpx.Response, stage: str) -> dict:
        """Parse a Graph API response, raising MetaAPIError on errors."""
        try:
            data = resp.json()
        except Exception:
            raise InstagramPublisherError(
                f"Non-JSON response from Graph API during '{stage}': {resp.text[:500]}"
            )

        if resp.is_error:
            error = data.get("error", {})
            raise MetaAPIError(
                code=resp.status_code,
                message=error.get("message", resp.text[:300]),
                fb_error_code=error.get("code"),
            )

        return data

    @staticmethod
    def _require_env(key: str) -> str:
        val = os.getenv(key)
        if not val:
            raise EnvironmentError(f"{key} must be set in .env")
        return val

    # ------------------------------------------------------------------
    # Token health check
    # ------------------------------------------------------------------

    async def check_token_health(self, warn_days: int = 7) -> None:
        """Log a warning if the access token is close to expiry.

        Uses the Graph API debug_token endpoint to check expiry.
        Does NOT raise — informational only.
        """
        app_id = os.getenv("FACEBOOK_APP_ID")
        app_secret = os.getenv("FACEBOOK_APP_SECRET")
        if not app_id or not app_secret:
            logger.debug("Skipping token health check — FACEBOOK_APP_ID/SECRET not set")
            return

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{_GRAPH_API_BASE}/debug_token",
                    params={
                        "input_token": self._access_token,
                        "access_token": f"{app_id}|{app_secret}",
                    },
                )
            data = resp.json().get("data", {})
            expires_at = data.get("expires_at")
            if expires_at:
                import time
                days_left = (expires_at - time.time()) / 86400
                if days_left < warn_days:
                    logger.warning(
                        "⚠️  Instagram access token expires in %.1f days! "
                        "Refresh it via: "
                        "https://developers.facebook.com/tools/explorer/",
                        days_left,
                    )
                else:
                    logger.info("Access token valid for %.1f days", days_left)
        except Exception as exc:
            logger.warning("Could not check token health: %s", exc)
