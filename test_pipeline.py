"""
test_pipeline.py

Manual end-to-end pipeline test. Runs the full pipeline (or individual stages)
WITHOUT needing Discord. Useful for local development and CI smoke testing.

Usage:
    # Full pipeline, dry-run (no Instagram upload):
    python test_pipeline.py --subreddit AskReddit --dry-run

    # Specific subreddit + tone:
    python test_pipeline.py --subreddit tifu --tone funny --dry-run

    # Full pipeline with real Instagram post:
    python test_pipeline.py --subreddit AskReddit

    # Test individual stages:
    python test_pipeline.py --stage screenshot --url <reddit_thread_url>
    python test_pipeline.py --stage tts --text "Hello this is a test script"
    python test_pipeline.py --stage video --screenshot <path> --audio <path> --subtitles <path>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from utils.logging_config import setup_logging

load_dotenv()


# ---------------------------------------------------------------------------
# Stage: Full pipeline
# ---------------------------------------------------------------------------

async def run_full_pipeline(subreddit: str, tone: str | None, dry_run: bool) -> None:
    from pipeline.orchestrator import run_pipeline

    print(f"\n{'='*60}")
    print(f"  AUTOPOSTER — Full Pipeline Test")
    print(f"  Subreddit: r/{subreddit}")
    print(f"  Tone:      {tone or 'random'}")
    print(f"  Dry run:   {dry_run}")
    print(f"{'='*60}\n")

    result = await run_pipeline(subreddit, tone=tone, dry_run=dry_run)

    print(f"\n{'='*60}")
    if result.success:
        print(f"  ✅ SUCCESS")
        print(f"  Job ID:     {result.job_id}")
        print(f"  Post:       {result.reddit_title}")
        print(f"  Reel URL:   {result.reel_url}")
    else:
        print(f"  ❌ FAILED")
        print(f"  Job ID:     {result.job_id}")
        print(f"  Stage:      {result.failed_stage}")
        print(f"  Error type: {result.error_type}")
        print(f"  Message:    {result.error_message}")
    print(f"{'='*60}\n")

    sys.exit(0 if result.success else 1)


# ---------------------------------------------------------------------------
# Stage: Screenshot only
# ---------------------------------------------------------------------------

async def run_screenshot_stage(url: str) -> None:
    from pipeline.screenshotter import Screenshotter

    print(f"\nTesting screenshotter on: {url}")
    out = Path("tmp/stage_test_screenshot.png")
    out.parent.mkdir(exist_ok=True)

    screenshotter = Screenshotter()
    result = await screenshotter.capture(url, out)
    print(f"✅ Screenshot saved: {result} ({result.stat().st_size / 1024:.1f} KB)")


# ---------------------------------------------------------------------------
# Stage: TTS only
# ---------------------------------------------------------------------------

async def run_tts_stage(text: str) -> None:
    from pipeline.tts_engine import generate_tts, load_word_boundaries

    print(f"\nTesting TTS on: {text[:60]}…")
    result = await generate_tts(text, Path("tmp/stage_test_tts"))

    print(f"✅ TTS result:")
    print(f"   Audio:    {result.audio_path} ({result.audio_path.stat().st_size / 1024:.1f} KB)")
    print(f"   Duration: {result.duration_sec:.2f}s")
    print(f"   Words:    {result.word_count}")

    wbs = load_word_boundaries(result.word_boundary_path)
    print(f"   Boundaries: {len(wbs)} words")
    print("\nFirst 5 word boundaries:")
    for wb in wbs[:5]:
        print(f"   [{wb.start_ms:6.0f}ms] {wb.word!r:20s} ({wb.duration_ms:.0f}ms)")

    # Also build subtitles
    from pipeline.subtitle_builder import build_ass_subtitles
    subs_path = Path("tmp/stage_test_tts/subtitles.ass")
    build_ass_subtitles(wbs, subs_path)
    print(f"\n✅ Subtitles: {subs_path}")


# ---------------------------------------------------------------------------
# Stage: Video composer only
# ---------------------------------------------------------------------------

def run_video_stage(screenshot: str, audio: str, subtitles: str) -> None:
    from pipeline.video_composer import VideoComposer
    from utils.video_validator import validate_video

    print(f"\nTesting video composer:")
    print(f"  Screenshot: {screenshot}")
    print(f"  Audio:      {audio}")
    print(f"  Subtitles:  {subtitles}")

    out = Path("tmp/stage_test_video.mp4")
    out.parent.mkdir(exist_ok=True)

    # We need to know the audio duration
    import subprocess, json
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", audio],
        capture_output=True, text=True
    )
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    print(f"  Audio duration: {duration:.2f}s")

    composer = VideoComposer()
    result = composer.compose(
        screenshot_path=Path(screenshot),
        audio_path=Path(audio),
        subtitles_path=Path(subtitles),
        audio_duration_sec=duration,
        output_path=out,
    )

    info = validate_video(result)
    print(f"\n✅ Video composed:")
    print(f"   File:       {result}")
    print(f"   Resolution: {info.width}x{info.height}")
    print(f"   Duration:   {info.duration_sec:.2f}s")
    print(f"   Codec:      {info.video_codec}/{info.audio_codec}")
    print(f"   Size:       {info.file_size_bytes / 1024 / 1024:.1f} MB")


# ---------------------------------------------------------------------------
# Stage: Script writer only
# ---------------------------------------------------------------------------

async def run_script_stage(title: str, tone: str) -> None:
    from pipeline.reddit_fetcher import RedditPost
    from pipeline.script_writer import ScriptWriter

    print(f"\nTesting script writer:")
    print(f"  Title: {title}")
    print(f"  Tone:  {tone}")

    post = RedditPost(
        id="test_script",
        title=title,
        permalink="/r/test/comments/test/",
        url="https://reddit.com/r/test/comments/test/",
        subreddit="test",
        score=10000,
        top_comments=[
            "This is the top comment.",
            "Another popular comment here.",
        ],
    )
    writer = ScriptWriter()
    script = await writer.generate(post, tone=tone)
    print(f"\n✅ Script ({len(script.split())} words):")
    print("-" * 60)
    print(script)
    print("-" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Autoposter manual test runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--stage", choices=["full", "screenshot", "tts", "video", "script"],
                        default="full", help="Which pipeline stage to test")
    parser.add_argument("--subreddit", default="AskReddit", help="Target subreddit")
    parser.add_argument("--tone", choices=["funny", "dramatic", "curious", "shocked", "unhinged"],
                        default=None)
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Skip Instagram upload (saves video locally)")
    parser.add_argument("--url", help="Reddit thread URL (for --stage screenshot)")
    parser.add_argument("--text", help="Text to synthesise (for --stage tts)")
    parser.add_argument("--title", default="TIFU by doing something incredibly dumb at work",
                        help="Post title (for --stage script)")
    parser.add_argument("--screenshot", help="Path to screenshot PNG (for --stage video)")
    parser.add_argument("--audio", help="Path to audio MP3 (for --stage video)")
    parser.add_argument("--subtitles", help="Path to .ass subtitles (for --stage video)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    setup_logging(level_str=args.log_level)

    if args.stage == "full":
        asyncio.run(run_full_pipeline(args.subreddit, args.tone, args.dry_run))

    elif args.stage == "screenshot":
        if not args.url:
            parser.error("--url is required for --stage screenshot")
        asyncio.run(run_screenshot_stage(args.url))

    elif args.stage == "tts":
        text = args.text or (
            "This is a test of the TTS engine. It should generate audio and word boundaries "
            "for accurate subtitle synchronisation without needing Whisper."
        )
        asyncio.run(run_tts_stage(text))

    elif args.stage == "video":
        for arg, name in [(args.screenshot, "--screenshot"), (args.audio, "--audio"),
                          (args.subtitles, "--subtitles")]:
            if not arg:
                parser.error(f"{name} is required for --stage video")
        run_video_stage(args.screenshot, args.audio, args.subtitles)

    elif args.stage == "script":
        asyncio.run(run_script_stage(args.title, args.tone or "funny"))


if __name__ == "__main__":
    main()
