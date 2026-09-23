"""
pipeline/video_composer.py

Compose the final 1080x1920 vertical Reel video from:
  1. Background gameplay clip (randomly selected + offset, looped if needed)
  2. Reddit screenshot overlay (upper-center, rounded corners)
  3. Burned-in .ass subtitles
  4. Voiceover MP3 audio track

Uses ffmpeg-python (a thin Python wrapper around ffmpeg) for full control
and performance. Falls back to subprocess if a filter graph is too complex
for the Python DSL.

Output: H.264 / AAC MP4 with +faststart flag for streaming.
"""

from __future__ import annotations

import logging
import math
import random
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import ffmpeg
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

_TARGET_WIDTH = 1080
_TARGET_HEIGHT = 1920
_FPS = 30
_VIDEO_CODEC = "libx264"
_AUDIO_CODEC = "aac"
_AUDIO_BITRATE = "128k"
_CRF = 23
_PRESET = "fast"


class VideoComposerError(RuntimeError):
    """Raised when video composition fails."""


class VideoComposer:
    """Compose the final Reel video from pipeline assets.

    Args:
        background_dir: Directory containing gameplay clip MP4s.
        width: Output video width in pixels.
        height: Output video height in pixels.
        fps: Output frame rate.
        crf: H.264 CRF quality (lower = better quality, larger file).
        audio_bitrate: AAC audio bitrate.
        tail_padding_sec: Extra seconds of video after voiceover ends.
        screenshot_width_fraction: Screenshot overlay width as fraction of frame width.
        screenshot_y_fraction: Screenshot top edge position as fraction of frame height.
        screenshot_corner_radius: Corner radius in pixels for the overlay.
    """

    def __init__(
        self,
        background_dir: str | Path = "assets/gameplay",
        width: int = _TARGET_WIDTH,
        height: int = _TARGET_HEIGHT,
        fps: int = _FPS,
        crf: int = _CRF,
        audio_bitrate: str = _AUDIO_BITRATE,
        tail_padding_sec: float = 1.0,
        screenshot_width_fraction: float = 0.92,
        screenshot_y_fraction: float | str = "middle",
        screenshot_corner_radius: int = 20,
    ) -> None:
        self.background_dir = Path(background_dir)
        self.width = width
        self.height = height
        self.fps = fps
        self.crf = crf
        self.audio_bitrate = audio_bitrate
        self.tail_padding_sec = tail_padding_sec
        self.screenshot_width_fraction = screenshot_width_fraction
        self.screenshot_y_fraction = screenshot_y_fraction
        self.screenshot_corner_radius = screenshot_corner_radius

    def compose(
        self,
        screenshot_path: Path,
        audio_path: Path,
        subtitles_path: Path,
        audio_duration_sec: float,
        output_path: Path,
    ) -> Path:
        """Render the final MP4.

        Args:
            screenshot_path: Reddit screenshot PNG.
            audio_path: Voiceover MP3.
            subtitles_path: Burned-in .ass subtitle file.
            audio_duration_sec: Voiceover duration in seconds.
            output_path: Destination MP4 path.

        Returns:
            *output_path* on success.

        Raises:
            VideoComposerError: On ffmpeg errors or validation failures.
        """
        self._validate_inputs(screenshot_path, audio_path, subtitles_path)

        video_duration = audio_duration_sec + self.tail_padding_sec
        background_clip = self._select_background_clip()
        logger.info(
            "Composing video: bg=%s, duration=%.2fs, output=%s",
            background_clip.name, video_duration, output_path,
        )

        # Prepare screenshot overlay with rounded corners
        overlay_path = output_path.parent / "screenshot_overlay.png"
        self._prepare_screenshot_overlay(screenshot_path, overlay_path)

        # Run ffmpeg composition
        self._run_ffmpeg(
            background_clip=background_clip,
            overlay_path=overlay_path,
            audio_path=audio_path,
            subtitles_path=subtitles_path,
            video_duration=video_duration,
            output_path=output_path,
        )

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise VideoComposerError(
                f"ffmpeg produced an empty output file: {output_path}"
            )

        logger.info(
            "Video composed: %s (%.1f MB)",
            output_path, output_path.stat().st_size / 1024 / 1024,
        )
        return output_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_inputs(
        self,
        screenshot: Path,
        audio: Path,
        subtitles: Path,
    ) -> None:
        for path, label in [
            (screenshot, "Screenshot"),
            (audio, "Audio"),
            (subtitles, "Subtitles"),
        ]:
            if not path.exists():
                raise VideoComposerError(f"{label} file not found: {path}")
            if path.stat().st_size == 0:
                raise VideoComposerError(f"{label} file is empty: {path}")

    def _select_background_clip(self) -> Path:
        """Pick a random gameplay clip from background_dir."""
        clips = list(self.background_dir.glob("*.mp4"))
        if not clips:
            raise VideoComposerError(
                f"No .mp4 files found in background directory: {self.background_dir}\n"
                "Add Subway Surfers (or similar) footage clips to assets/gameplay/\n"
                "or run: python tools/download_gameplay.py"
            )
        return random.choice(clips)

    def _prepare_screenshot_overlay(
        self,
        src: Path,
        dst: Path,
    ) -> None:
        """Resize screenshot to target overlay width and apply rounded corners."""
        overlay_width = int(self.width * self.screenshot_width_fraction)

        with Image.open(src) as img:
            # Convert to RGBA for transparency support
            img = img.convert("RGBA")

            # Resize maintaining aspect ratio
            orig_w, orig_h = img.size
            scale = overlay_width / orig_w
            new_h = int(orig_h * scale)
            img = img.resize((overlay_width, new_h), Image.LANCZOS)

            # Apply rounded corner mask
            if self.screenshot_corner_radius > 0:
                img = self._apply_rounded_corners(img, self.screenshot_corner_radius)

            img.save(dst, "PNG")
            logger.debug(
                "Overlay prepared: %dx%d → %s", overlay_width, new_h, dst
            )

    @staticmethod
    def _apply_rounded_corners(img: Image.Image, radius: int) -> Image.Image:
        """Apply a rounded rectangle mask to an RGBA image."""
        mask = Image.new("L", img.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle([(0, 0), img.size], radius=radius, fill=255)
        img.putalpha(mask)
        return img

    def _run_ffmpeg(
        self,
        background_clip: Path,
        overlay_path: Path,
        audio_path: Path,
        subtitles_path: Path,
        video_duration: float,
        output_path: Path,
    ) -> None:
        """Build and execute the ffmpeg filter graph."""
        # Probe background clip to get its duration for random offset
        bg_duration = self._probe_duration(background_clip)
        max_offset = max(0.0, bg_duration - video_duration - 1.0)
        start_offset = random.uniform(0.0, max_offset) if max_offset > 1.0 else 0.0
        logger.debug("Background clip offset: %.2fs", start_offset)

        # Overlay position (support 'middle' / 'center' or numeric fraction)
        overlay_x = int((self.width - int(self.width * self.screenshot_width_fraction)) / 2)
        if isinstance(self.screenshot_y_fraction, str) and self.screenshot_y_fraction.lower() in ("middle", "center"):
            with Image.open(overlay_path) as oimg:
                overlay_h = oimg.height
            overlay_y = max(0, int((self.height - overlay_h) / 2))
        else:
            try:
                frac = float(self.screenshot_y_fraction)
                overlay_y = int(self.height * frac)
            except (ValueError, TypeError):
                with Image.open(overlay_path) as oimg:
                    overlay_h = oimg.height
                overlay_y = max(0, int((self.height - overlay_h) / 2))

        # We use the subtitles filter which reads the .ass file directly.
        # On Windows, backslashes in the path must be escaped for ffmpeg's filter syntax.
        subs_path_escaped = str(subtitles_path.resolve()).replace("\\", "/").replace(":", "\\:")

        # Build the complex filter chain via subprocess for reliability
        cmd = [
            "ffmpeg", "-y",

            # --- Background video input (looped/trimmed) ---
            "-ss", str(start_offset),
            "-i", str(background_clip),

            # --- Screenshot overlay input ---
            "-i", str(overlay_path),

            # --- Audio input ---
            "-i", str(audio_path),

            # --- Filter graph ---
            "-filter_complex",
            (
                # 1. Scale + crop background to 1080x1920
                f"[0:v]scale={self.width}:{self.height}:force_original_aspect_ratio=increase,"
                f"crop={self.width}:{self.height},"
                f"fps={self.fps}[bg];"

                # 2. Place screenshot overlay
                f"[bg][1:v]overlay={overlay_x}:{overlay_y}:format=auto[with_overlay];"

                # 3. Burn in .ass subtitles
                f"[with_overlay]ass='{subs_path_escaped}'[outv]"
            ),

            # --- Map outputs ---
            "-map", "[outv]",
            "-map", "2:a",

            # --- Duration trim ---
            "-t", str(video_duration),

            # --- Video encoding ---
            "-c:v", _VIDEO_CODEC,
            "-crf", str(self.crf),
            "-preset", _PRESET,
            "-profile:v", "high",
            "-level", "4.1",
            "-pix_fmt", "yuv420p",

            # --- Audio encoding ---
            "-c:a", _AUDIO_CODEC,
            "-b:a", self.audio_bitrate,

            # --- Container ---
            "-movflags", "+faststart",
            "-shortest",

            str(output_path),
        ]

        logger.debug("Running ffmpeg: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            # Surface the ffmpeg stderr for debugging
            stderr_tail = exc.stderr[-2000:] if exc.stderr else "(no stderr)"
            raise VideoComposerError(
                f"ffmpeg exited with code {exc.returncode}:\n{stderr_tail}"
            ) from exc
        except subprocess.TimeoutExpired:
            raise VideoComposerError("ffmpeg timed out after 5 minutes")
        except FileNotFoundError:
            raise VideoComposerError(
                "ffmpeg not found. Install FFmpeg and add it to your PATH."
            )

    @staticmethod
    def _probe_duration(video_path: Path) -> float:
        """Return the duration of a video file in seconds via ffprobe."""
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-print_format", "json",
                    "-show_format",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            import json
            data = json.loads(result.stdout)
            return float(data["format"].get("duration", 60.0))
        except Exception as exc:
            logger.warning("Could not probe duration of %s: %s — assuming 60s", video_path, exc)
            return 60.0


if __name__ == "__main__":
    import sys
    from utils.logging_config import setup_logging
    from utils.video_validator import validate_video

    setup_logging()

    def _test(screenshot: str, audio: str, subtitles: str, output: str = "tmp/test_output.mp4") -> None:
        composer = VideoComposer()
        out = composer.compose(
            screenshot_path=Path(screenshot),
            audio_path=Path(audio),
            subtitles_path=Path(subtitles),
            audio_duration_sec=30.0,
            output_path=Path(output),
        )
        info = validate_video(out)
        print(f"\n✅ Video composed: {out}")
        print(f"   Resolution: {info.width}x{info.height}")
        print(f"   Duration:   {info.duration_sec:.2f}s")
        print(f"   Codec:      {info.video_codec}/{info.audio_codec}")
        print(f"   Size:       {info.file_size_bytes / 1024 / 1024:.1f} MB")

    if len(sys.argv) < 4:
        print("Usage: python -m pipeline.video_composer <screenshot.png> <audio.mp3> <subtitles.ass>")
        sys.exit(1)

    _test(sys.argv[1], sys.argv[2], sys.argv[3])
