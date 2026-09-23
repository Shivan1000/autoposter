"""
pipeline/subtitle_builder.py

Convert edge-tts WordBoundary timing data into a styled .ass subtitle file.

Design goals:
  - Use ground-truth timing from edge-tts — no Whisper re-transcription needed
  - Chunk words into short phrases (3–5 words) for dynamic mobile-friendly display
  - Safe margins: top 200px (Reddit overlay zone), bottom 320px (Instagram UI zone)
  - Bold, outlined font legible on any background
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pipeline.tts_engine import WordBoundary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# .ass file template
# ---------------------------------------------------------------------------

_ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes
YCbCr Matrix: None

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{primary_color},{secondary_color},{outline_color},{back_color},{bold},{italic},0,0,100,100,0,0,1,{outline_width},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

_ASS_EVENT_LINE = (
    "Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n"
)


@dataclass
class SubtitleConfig:
    """Visual parameters for subtitle rendering."""
    font_name: str = "Arial Black"
    font_size: int = 78
    primary_color: str = "&H00FFFFFF"   # white (AABBGGRR)
    secondary_color: str = "&H00FFFFFF"
    outline_color: str = "&H00000000"   # black
    back_color: str = "&H80000000"      # semi-transparent black shadow
    bold: int = -1                       # -1 = bold, 0 = normal
    italic: int = 0
    outline_width: float = 5.0
    shadow: float = 2.0
    # Alignment: 2 = bottom-center (standard subtitle position)
    alignment: int = 2
    margin_l: int = 60
    margin_r: int = 60
    # Bottom margin (pixels from bottom)
    margin_v: int = 500
    words_per_chunk: int = 3


@dataclass
class SubtitleChunk:
    """A timed group of words shown as one subtitle line."""
    text: str
    start_ms: float
    end_ms: float


class SubtitleBuilderError(ValueError):
    """Raised when subtitle generation fails."""


def build_ass_subtitles(
    word_boundaries: list[WordBoundary],
    output_path: Path,
    config: Optional[SubtitleConfig] = None,
    end_padding_ms: float = 300.0,
) -> Path:
    """Build a .ass subtitle file from word-boundary timing data.

    Args:
        word_boundaries: List of :class:`WordBoundary` from the TTS engine.
        output_path: Where to write the .ass file.
        config: Visual subtitle parameters. Uses defaults if None.
        end_padding_ms: Extra time added to the end of the last chunk (ms).

    Returns:
        *output_path* on success.

    Raises:
        SubtitleBuilderError: If word_boundaries is empty or output fails.
    """
    if not word_boundaries:
        raise SubtitleBuilderError("Cannot build subtitles: word_boundaries list is empty")

    cfg = config or SubtitleConfig()
    chunks = _chunk_words(word_boundaries, cfg.words_per_chunk, end_padding_ms)
    logger.info(
        "Building .ass subtitles: %d words → %d chunks → %s",
        len(word_boundaries), len(chunks), output_path,
    )

    ass_content = _render_ass(chunks, cfg)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(ass_content, encoding="utf-8-sig")  # BOM for compatibility

    logger.info("Subtitles written: %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _chunk_words(
    boundaries: list[WordBoundary],
    words_per_chunk: int,
    end_padding_ms: float,
) -> list[SubtitleChunk]:
    """Group word boundaries into fixed-size phrase chunks."""
    chunks: list[SubtitleChunk] = []
    i = 0
    while i < len(boundaries):
        group = boundaries[i : i + words_per_chunk]
        text = " ".join(wb.word for wb in group)
        start_ms = group[0].start_ms
        # End of this chunk = start of next chunk - 1ms, or last word end + padding
        if i + words_per_chunk < len(boundaries):
            end_ms = boundaries[i + words_per_chunk].start_ms - 1
        else:
            last = group[-1]
            end_ms = last.start_ms + last.duration_ms + end_padding_ms

        chunks.append(SubtitleChunk(text=text, start_ms=start_ms, end_ms=end_ms))
        i += words_per_chunk
    return chunks


def _render_ass(chunks: list[SubtitleChunk], cfg: SubtitleConfig) -> str:
    """Render chunks into a complete .ass file string."""
    header = _ASS_HEADER.format(
        font_name=cfg.font_name,
        font_size=cfg.font_size,
        primary_color=cfg.primary_color,
        secondary_color=cfg.secondary_color,
        outline_color=cfg.outline_color,
        back_color=cfg.back_color,
        bold=cfg.bold,
        italic=cfg.italic,
        outline_width=cfg.outline_width,
        shadow=cfg.shadow,
        alignment=cfg.alignment,
        margin_l=cfg.margin_l,
        margin_r=cfg.margin_r,
        margin_v=cfg.margin_v,
    )
    events = "".join(
        _ASS_EVENT_LINE.format(
            start=_ms_to_ass_time(chunk.start_ms),
            end=_ms_to_ass_time(chunk.end_ms),
            text=_escape_ass(chunk.text),
        )
        for chunk in chunks
    )
    return header + events


def _ms_to_ass_time(ms: float) -> str:
    """Convert milliseconds to ASS time format H:MM:SS.cs (centiseconds)."""
    total_cs = int(ms / 10)
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass(text: str) -> str:
    """Escape characters that have special meaning in .ass files."""
    return text.replace("{", r"\{").replace("}", r"\}")


if __name__ == "__main__":
    import asyncio
    from pathlib import Path
    from utils.logging_config import setup_logging
    from pipeline.tts_engine import generate_tts, load_word_boundaries

    setup_logging()

    _SAMPLE = (
        "This is a test of the subtitle builder. "
        "Each phrase should appear as a short chunk on screen. "
        "The timing comes directly from edge-tts, so it is perfectly synced."
    )

    async def _test() -> None:
        result = await generate_tts(_SAMPLE, Path("tmp/sub_test"))
        wbs = load_word_boundaries(result.word_boundary_path)
        out = Path("tmp/sub_test/subtitles.ass")
        build_ass_subtitles(wbs, out)
        print(f"✅ ASS file written: {out}")
        print(out.read_text(encoding="utf-8-sig")[:800])

    asyncio.run(_test())
