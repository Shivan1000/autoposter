"""
utils/video_validator.py

Run ffprobe on a completed video file and assert it meets Instagram Reels
requirements before we attempt to upload it.

Instagram Reels limits (as of 2024):
  - Duration: 3 – 90 seconds
  - Resolution: minimum 540x960; we target 1080x1920
  - Aspect ratio: 9:16
  - Codec: H.264 video, AAC audio
  - File size: ≤ 1 GB (in practice keep well under 100 MB)
  - Container: MP4
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ---- Instagram Reels hard limits ----------------------------------------
_MIN_DURATION_SEC = 3.0
_MAX_DURATION_SEC = 90.0
_MAX_FILE_SIZE_BYTES = 900 * 1024 * 1024  # 900 MB (leave headroom)
_EXPECTED_WIDTH = 1080
_EXPECTED_HEIGHT = 1920
_EXPECTED_VIDEO_CODEC = "h264"
_EXPECTED_AUDIO_CODEC = "aac"


class VideoValidationError(ValueError):
    """Raised when a rendered video fails a pre-upload sanity check."""


@dataclass
class VideoInfo:
    width: int
    height: int
    duration_sec: float
    video_codec: str
    audio_codec: str
    file_size_bytes: int
    fps: float


def validate_video(path: Path | str) -> VideoInfo:
    """Run ffprobe on *path* and assert it meets Reels constraints.

    Args:
        path: Absolute or relative path to the MP4 file.

    Returns:
        A :class:`VideoInfo` dataclass with the probe results.

    Raises:
        VideoValidationError: If any constraint is violated.
        FileNotFoundError: If the file doesn't exist.
        RuntimeError: If ffprobe is not installed or fails unexpectedly.
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Video file not found: {path}")

    file_size = path.stat().st_size
    if file_size == 0:
        raise VideoValidationError(f"Video file is empty (0 bytes): {path}")

    probe_data = _run_ffprobe(path)

    video_stream = _find_stream(probe_data, "video")
    audio_stream = _find_stream(probe_data, "audio")

    if video_stream is None:
        raise VideoValidationError(f"No video stream found in: {path}")
    if audio_stream is None:
        raise VideoValidationError(f"No audio stream found in: {path}")

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    video_codec = video_stream.get("codec_name", "").lower()
    audio_codec = audio_stream.get("codec_name", "").lower()

    # Duration — prefer format-level, fall back to stream-level
    try:
        duration = float(probe_data["format"].get("duration", 0))
    except (KeyError, TypeError, ValueError):
        duration = float(video_stream.get("duration", 0))

    # FPS — expressed as a fraction string e.g. "30000/1001"
    fps = _parse_fps(video_stream.get("r_frame_rate", "0/1"))

    info = VideoInfo(
        width=width,
        height=height,
        duration_sec=duration,
        video_codec=video_codec,
        audio_codec=audio_codec,
        file_size_bytes=file_size,
        fps=fps,
    )

    logger.debug(
        "ffprobe result for %s: %dx%d, %.2fs, %s/%s, %.0f fps, %.1f MB",
        path.name, width, height, duration, video_codec, audio_codec,
        fps, file_size / 1024 / 1024,
    )

    _assert_constraints(info, path)
    return info


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_ffprobe(path: Path) -> dict:
    """Run ffprobe and return the parsed JSON output."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "ffprobe not found. Install FFmpeg and ensure it's in your PATH."
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"ffprobe failed with code {exc.returncode}: {exc.stderr.strip()}"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffprobe timed out on: {path}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse ffprobe output: {exc}") from exc


def _find_stream(probe_data: dict, codec_type: str) -> dict | None:
    """Return the first stream of the given codec_type, or None."""
    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") == codec_type:
            return stream
    return None


def _parse_fps(fps_str: str) -> float:
    """Parse a fraction string like '30000/1001' into a float."""
    try:
        if "/" in fps_str:
            num, den = fps_str.split("/")
            return float(num) / float(den) if float(den) else 0.0
        return float(fps_str)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _assert_constraints(info: VideoInfo, path: Path) -> None:
    """Raise VideoValidationError if any constraint is violated."""
    errors: list[str] = []

    if info.duration_sec < _MIN_DURATION_SEC:
        errors.append(
            f"Duration {info.duration_sec:.1f}s is below minimum {_MIN_DURATION_SEC}s"
        )
    if info.duration_sec > _MAX_DURATION_SEC:
        errors.append(
            f"Duration {info.duration_sec:.1f}s exceeds maximum {_MAX_DURATION_SEC}s"
        )
    if info.width != _EXPECTED_WIDTH or info.height != _EXPECTED_HEIGHT:
        errors.append(
            f"Resolution {info.width}x{info.height} != expected {_EXPECTED_WIDTH}x{_EXPECTED_HEIGHT}"
        )
    if info.video_codec != _EXPECTED_VIDEO_CODEC:
        errors.append(
            f"Video codec '{info.video_codec}' != expected '{_EXPECTED_VIDEO_CODEC}'"
        )
    if info.audio_codec != _EXPECTED_AUDIO_CODEC:
        errors.append(
            f"Audio codec '{info.audio_codec}' != expected '{_EXPECTED_AUDIO_CODEC}'"
        )
    if info.file_size_bytes > _MAX_FILE_SIZE_BYTES:
        errors.append(
            f"File size {info.file_size_bytes / 1024 / 1024:.1f} MB exceeds "
            f"limit {_MAX_FILE_SIZE_BYTES / 1024 / 1024:.0f} MB"
        )

    if errors:
        raise VideoValidationError(
            f"Video validation failed for {path.name}:\n"
            + "\n".join(f"  • {e}" for e in errors)
        )


if __name__ == "__main__":
    import sys
    import logging
    logging.basicConfig(level=logging.DEBUG)

    if len(sys.argv) < 2:
        print("Usage: python -m utils.video_validator <path_to_video.mp4>")
        sys.exit(1)

    target = Path(sys.argv[1])
    try:
        video_info = validate_video(target)
        print(f"✅ Video OK: {video_info}")
    except (VideoValidationError, FileNotFoundError, RuntimeError) as e:
        print(f"❌ {e}")
        sys.exit(1)
