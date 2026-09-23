"""
tools/download_gameplay.py

Utility to seed the assets/gameplay/ directory with background footage clips.

Uses yt-dlp to download gameplay footage from YouTube. We recommend using
royalty-free or Creative Commons licensed footage.

Usage:
    # Download default gameplay clips:
    python tools/download_gameplay.py

    # Download from a specific YouTube URL:
    python tools/download_gameplay.py --url "https://www.youtube.com/watch?v=..."

    # Download and split into 60-second segments:
    python tools/download_gameplay.py --url "..." --segment 60

    # List currently available clips:
    python tools/download_gameplay.py --list

IMPORTANT: Only download footage you have the right to use.
Creative Commons gameplay compilations are available on YouTube.
Search for: "Subway Surfers gameplay no copyright" or similar.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT_DIR = Path("assets/gameplay")

# Suggested free-to-use gameplay URLs (search YouTube for CC-licensed content)
# Replace these with videos you have confirmed rights to use.
_DEFAULT_URLS: list[str] = [
    # Example: add your own royalty-free gameplay URL here
    # "https://www.youtube.com/watch?v=EXAMPLE1",
]

_YTDLP_BASE_ARGS = [
    "yt-dlp",
    "--format", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
    "--merge-output-format", "mp4",
    "--no-playlist",
    "--retries", "5",
]


def download_clip(url: str, output_dir: Path, segment_sec: int | None = None) -> list[Path]:
    """Download a YouTube video and optionally split into segments.

    Args:
        url: YouTube URL.
        output_dir: Directory to save the downloaded clip.
        segment_sec: If set, split the video into segments of this length (seconds).

    Returns:
        List of downloaded/created file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / "%(title).50s_%(id)s.%(ext)s")

    cmd = _YTDLP_BASE_ARGS + ["--output", output_template, url]

    if segment_sec:
        # Use yt-dlp's --download-sections or post-process with ffmpeg
        # We'll download first, then split
        logger.info("Downloading: %s", url)
    else:
        logger.info("Downloading: %s", url)

    try:
        result = subprocess.run(cmd, check=True, timeout=600)
    except FileNotFoundError:
        print("❌ yt-dlp not found. Install it with: pip install yt-dlp")
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(f"❌ yt-dlp failed with code {exc.returncode}")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("❌ Download timed out after 10 minutes")
        sys.exit(1)

    # Find the downloaded file
    mp4_files = sorted(output_dir.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not mp4_files:
        print("❌ No MP4 files found after download")
        sys.exit(1)

    downloaded = mp4_files[0]
    logger.info("Downloaded: %s (%.1f MB)", downloaded.name, downloaded.stat().st_size / 1024 / 1024)

    if segment_sec:
        segments = _split_into_segments(downloaded, segment_sec)
        downloaded.unlink()  # Remove the original unsegmented file
        return segments

    return [downloaded]


def _split_into_segments(video_path: Path, segment_sec: int) -> list[Path]:
    """Split a video into fixed-length segments using ffmpeg."""
    output_dir = video_path.parent
    stem = video_path.stem
    output_pattern = str(output_dir / f"{stem}_segment_%03d.mp4")

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(segment_sec),
        "-reset_timestamps", "1",
        output_pattern,
    ]

    logger.info("Splitting %s into %ds segments…", video_path.name, segment_sec)
    try:
        subprocess.run(cmd, check=True, timeout=300)
    except subprocess.CalledProcessError as exc:
        print(f"❌ ffmpeg segment split failed: {exc}")
        sys.exit(1)

    segments = sorted(output_dir.glob(f"{stem}_segment_*.mp4"))
    logger.info("Created %d segments", len(segments))
    return segments


def list_clips(output_dir: Path) -> None:
    """Print all available gameplay clips."""
    clips = sorted(output_dir.glob("*.mp4"))
    if not clips:
        print(f"No clips found in {output_dir}/")
        print("Run this script with a YouTube URL to download footage.")
        return

    print(f"\n📹 Available gameplay clips in {output_dir}/:")
    total_size = 0
    for clip in clips:
        size = clip.stat().st_size
        total_size += size
        print(f"  {clip.name:60s}  {size / 1024 / 1024:6.1f} MB")
    print(f"\n  Total: {len(clips)} clips, {total_size / 1024 / 1024:.1f} MB")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Download gameplay footage for autoposter")
    parser.add_argument("--url", help="YouTube URL to download")
    parser.add_argument("--output-dir", default=str(_DEFAULT_OUTPUT_DIR),
                        help=f"Output directory (default: {_DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--segment", type=int, default=None,
                        help="Split into segments of N seconds after download")
    parser.add_argument("--list", action="store_true", help="List available clips")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    if args.list:
        list_clips(output_dir)
        return

    if args.url:
        files = download_clip(args.url, output_dir, args.segment)
        print(f"\n✅ Downloaded {len(files)} file(s):")
        for f in files:
            print(f"  {f}")
    elif _DEFAULT_URLS:
        for url in _DEFAULT_URLS:
            files = download_clip(url, output_dir, args.segment)
            print(f"✅ Downloaded {len(files)} file(s) from {url}")
    else:
        print("No URL specified and no default URLs configured.")
        print("Add royalty-free gameplay URLs to _DEFAULT_URLS in this file,")
        print("or run: python tools/download_gameplay.py --url <youtube_url>")
        print("\nSearching YouTube for 'Subway Surfers gameplay no copyright'")
        print("is a good starting point.")
        sys.exit(1)


if __name__ == "__main__":
    main()
