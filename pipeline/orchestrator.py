"""
pipeline/orchestrator.py

Top-level pipeline runner that ties all stages together.

Design principles:
  - Each stage is independently wrapped in try/except with specific error types
  - Temp files live in a per-job JobTempDir (cleaned on success, kept on failure)
  - The Reddit post is NOT marked as processed until Instagram publish succeeds
  - Returns a structured PipelineResult (success/failure, reel URL, error stage)
  - Does NOT know about Discord — the bot calls run_pipeline() and reads the result
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

from pipeline.caption_generator import CaptionGenerator
from pipeline.dedup_store import DedupStore
from pipeline.instagram_browser import InstagramBrowserPublisher, InstagramBrowserError
from pipeline.reddit_scraper import RedditScraper, RedditScraperError, RedditPost
from pipeline.script_writer import ScriptWriter, ScriptWriterError
from pipeline.subtitle_builder import SubtitleConfig, build_ass_subtitles
from pipeline.tts_engine import TTSError, generate_tts, load_word_boundaries
from pipeline.video_composer import VideoComposer, VideoComposerError
from utils.logging_config import JobLoggerAdapter, setup_logging
from utils.temp_manager import JobTempDir
from utils.video_validator import VideoValidationError, validate_video

load_dotenv()
logger = logging.getLogger(__name__)


def _load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """The outcome of a single pipeline run."""
    success: bool
    job_id: str
    subreddit: str
    reddit_post_id: Optional[str] = None
    reddit_title: Optional[str] = None
    reel_url: Optional[str] = None
    failed_stage: Optional[str] = None
    error_message: Optional[str] = None
    error_type: Optional[str] = None


# ---------------------------------------------------------------------------
# Main pipeline function
# ---------------------------------------------------------------------------

async def run_pipeline(
    subreddit: str,
    tone: Optional[str] = None,
    dry_run: bool = False,
    config_path: str = "config.yaml",
) -> PipelineResult:
    """Execute the full Reddit → Instagram Reels pipeline.

    Args:
        subreddit: Target subreddit (without r/).
        tone: Caption/script tone override. Randomly chosen from config if None.
        dry_run: If True, skip Instagram upload and return without posting.
        config_path: Path to config.yaml.

    Returns:
        A :class:`PipelineResult` with full outcome details.
    """
    job_id = uuid.uuid4().hex[:12]
    cfg = _load_config(config_path)
    job_logger = JobLoggerAdapter(logger, job_id)
    job_logger.info("Pipeline started for r/%s (tone=%s, dry_run=%s)", subreddit, tone, dry_run)

    # Randomly choose tone from presets if not specified
    if tone is None:
        tone_presets = cfg.get("llm", {}).get("tone_presets", ["funny"])
        tone = random.choice(tone_presets)
        job_logger.info("Selected random tone: %s", tone)

    result = PipelineResult(
        success=False,
        job_id=job_id,
        subreddit=subreddit,
    )

    # ---- Initialise shared services ----
    dedup_store = DedupStore(cfg.get("storage", {}).get("db_path", "data/dedup.db"))
    await dedup_store.init()

    async with JobTempDir(job_id=job_id) as tmp:
        try:
            # ----------------------------------------------------------------
            # STAGE 1: Scrape Reddit + Screenshot (single browser session)
            # ----------------------------------------------------------------
            stage = "reddit_scrape"
            job_logger.info("[Stage 1/7] Scraping Reddit post from r/%s + taking screenshot", subreddit)
            screenshot_path = tmp.subpath("screenshot.png")
            scraper = RedditScraper(
                time_filter=cfg.get("reddit", {}).get("time_filter", "day"),
                candidate_pool=cfg.get("reddit", {}).get("candidate_pool", 10),
                allow_nsfw=cfg.get("reddit", {}).get("allow_nsfw", False),
                top_comments_count=cfg.get("reddit", {}).get("top_comments_count", 5),
                min_score=cfg.get("reddit", {}).get("min_score", 100),
            )
            post: RedditPost = await scraper.fetch_and_screenshot(
                subreddit, screenshot_path, dedup_store
            )
            result.reddit_post_id = post.id
            result.reddit_title = post.title
            job_logger.info("Post selected: [%s] %s", post.id, post.title[:60])

            # ----------------------------------------------------------------
            # STAGE 3: Generate voiceover script
            # ----------------------------------------------------------------
            stage = "script_writer"
            job_logger.info("[Stage 2/7] Generating voiceover script (tone=%s)", tone)
            script_writer = ScriptWriter(
                model=cfg.get("llm", {}).get("script_model", "gemini-flash-latest"),
                max_words=cfg.get("tts", {}).get("max_words", 130),
                max_tokens=cfg.get("llm", {}).get("max_tokens", 2048),
                temperature=cfg.get("llm", {}).get("temperature", 1.0),
            )
            script = await script_writer.generate(post, tone=tone)

            # ----------------------------------------------------------------
            # STAGE 4: Text-to-speech
            # ----------------------------------------------------------------
            stage = "tts"
            job_logger.info("[Stage 3/7] Generating TTS audio")
            tts_result = await generate_tts(
                script=script,
                output_dir=tmp.path,
                voice=cfg.get("tts", {}).get("voice", "en-US-GuyNeural"),
                rate=cfg.get("tts", {}).get("rate", "+0%"),
                max_duration_sec=cfg.get("tts", {}).get("max_duration_sec", 60.0),
            )

            # ----------------------------------------------------------------
            # STAGE 5: Build subtitles
            # ----------------------------------------------------------------
            stage = "subtitle_builder"
            job_logger.info("[Stage 4/7] Building subtitles from word boundaries")
            word_boundaries = load_word_boundaries(tts_result.word_boundary_path)
            subs_cfg = cfg.get("subtitles", {})
            subtitle_config = SubtitleConfig(
                font_name=subs_cfg.get("font_name", "Arial Black"),
                font_size=subs_cfg.get("font_size", 78),
                primary_color=subs_cfg.get("primary_color", "&H00FFFFFF"),
                outline_color=subs_cfg.get("outline_color", "&H00000000"),
                outline_width=float(subs_cfg.get("outline_width", 5.0)),
                shadow=float(subs_cfg.get("shadow", 2.0)),
                margin_v=subs_cfg.get("margin_bottom", 500),
                margin_l=subs_cfg.get("margin_left", 60),
                margin_r=subs_cfg.get("margin_right", 60),
                words_per_chunk=subs_cfg.get("words_per_chunk", 3),
            )
            subtitles_path = tmp.subpath("subtitles.ass")
            build_ass_subtitles(word_boundaries, subtitles_path, subtitle_config)

            # ----------------------------------------------------------------
            # STAGE 6: Generate Instagram caption
            # ----------------------------------------------------------------
            stage = "caption_generator"
            job_logger.info("[Stage 5/7] Generating Instagram caption")
            cap_gen = CaptionGenerator(
                model=cfg.get("llm", {}).get("caption_model", "gemini-flash-latest"),
                max_tokens=cfg.get("llm", {}).get("max_tokens", 1024),
                temperature=cfg.get("llm", {}).get("temperature", 1.0),
                tone_presets=cfg.get("llm", {}).get("tone_presets", ["funny"]),
            )
            caption_obj = await cap_gen.generate(post, script, tone=tone)

            # ----------------------------------------------------------------
            # STAGE 7: Compose video
            # ----------------------------------------------------------------
            stage = "video_composer"
            job_logger.info("[Stage 6/7] Composing final video")
            vid_cfg = cfg.get("video", {})
            composer = VideoComposer(
                background_dir=vid_cfg.get("background_dir", "assets/gameplay"),
                crf=vid_cfg.get("crf", 23),
                audio_bitrate=vid_cfg.get("audio_bitrate", "128k"),
                tail_padding_sec=vid_cfg.get("tail_padding_sec", 1.0),
                screenshot_width_fraction=vid_cfg.get("screenshot_width_fraction", 0.92),
                screenshot_y_fraction=vid_cfg.get("screenshot_y_fraction", 0.08),
                screenshot_corner_radius=vid_cfg.get("screenshot_corner_radius", 20),
            )
            output_path = tmp.subpath("final_reel.mp4")
            composer.compose(
                screenshot_path=screenshot_path,
                audio_path=tts_result.audio_path,
                subtitles_path=subtitles_path,
                audio_duration_sec=tts_result.duration_sec,
                output_path=output_path,
            )

            # Validate before upload
            validate_video(output_path)
            job_logger.info("Video validation passed: %s", output_path.name)

            # ----------------------------------------------------------------
            # STAGE 8: Publish to Instagram
            # ----------------------------------------------------------------
            stage = "instagram_publish"

            if dry_run:
                job_logger.info(
                    "[Stage 7/7] DRY RUN — skipping Instagram upload. Video: %s",
                    output_path.resolve(),
                )
                # In dry-run mode, copy the video to a persistent location
                dry_run_output = Path("tmp") / f"dry_run_{job_id}.mp4"
                import shutil
                shutil.copy2(output_path, dry_run_output)
                job_logger.info("Dry-run video saved to: %s", dry_run_output.resolve())

                result.success = True
                result.reel_url = f"file://{dry_run_output.resolve()}"
                return result

            job_logger.info("[Stage 7/7] Publishing to Instagram via browser")
            publisher = InstagramBrowserPublisher(
                headless=cfg.get("instagram", {}).get("headless", False),
            )
            publish_result = await publisher.publish(
                video_path=output_path,
                caption=caption_obj.full_caption,
                job_id=job_id,
            )

            if not publish_result.success:
                raise InstagramBrowserError(
                    publish_result.error_message or "Instagram upload failed"
                )

            # ---- Mark as processed ONLY after successful publish ----
            await dedup_store.mark_processed(
                reddit_id=post.id,
                subreddit=post.subreddit,
                title=post.title,
                reel_url=publish_result.reel_url,
            )

            result.success = True
            result.reel_url = publish_result.reel_url
            job_logger.info("✅ Pipeline complete! Reel: %s", publish_result.reel_url)

        # ---- Per-stage error handling ----
        except ValueError as exc:
            result.failed_stage = stage
            result.error_message = str(exc)
            result.error_type = type(exc).__name__
            job_logger.error("Pipeline failed at [%s]: %s", stage, exc)
        except (RedditScraperError, ScriptWriterError, TTSError,
                VideoComposerError, VideoValidationError, InstagramBrowserError) as exc:
            result.failed_stage = stage
            result.error_message = str(exc)
            result.error_type = type(exc).__name__
            job_logger.error("Pipeline failed at [%s]: %s", stage, exc)
        except Exception as exc:
            result.failed_stage = stage
            result.error_message = f"Unexpected error: {exc}"
            result.error_type = type(exc).__name__
            job_logger.exception("Unexpected error at [%s]", stage)

    return result


if __name__ == "__main__":
    import sys
    from utils.logging_config import setup_logging

    setup_logging()

    sub = sys.argv[1] if len(sys.argv) > 1 else "AskReddit"
    t = sys.argv[2] if len(sys.argv) > 2 else None

    res = asyncio.run(run_pipeline(sub, tone=t, dry_run=True))
    print(f"\nResult: {'✅ SUCCESS' if res.success else '❌ FAILED'}")
    if res.success:
        print(f"  Reel URL: {res.reel_url}")
    else:
        print(f"  Failed stage: {res.failed_stage}")
        print(f"  Error ({res.error_type}): {res.error_message}")
