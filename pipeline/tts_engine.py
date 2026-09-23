"""
pipeline/tts_engine.py

Convert a text script to speech using edge-tts (Microsoft Edge TTS).

edge-tts produces:
  1. An MP3 audio file
  2. WordBoundary events — word-level timing metadata (offset + duration in ms)
     for accurate subtitle generation without re-transcription.

The WordBoundary JSON is saved alongside the MP3 and returned for use by
subtitle_builder.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import edge_tts
import httpx
from edge_tts import Communicate
from edge_tts.exceptions import EdgeTTSException

from utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

_DEFAULT_VOICE = "en-US-ChristopherNeural"
_DEFAULT_RATE = "+5%"
_MAX_DURATION_SEC = 60.0
_MIN_DURATION_SEC = 2.0


class TTSError(RuntimeError):
    """Raised when TTS generation fails."""


@dataclass
class TTSResult:
    """Output from the TTS engine."""
    audio_path: Path          # .mp3 file
    word_boundary_path: Path  # .json file of WordBoundary events
    duration_sec: float       # Approximate audio duration in seconds
    word_count: int


@dataclass
class WordBoundary:
    """A single word boundary event from edge-tts or ElevenLabs."""
    word: str
    start_ms: float   # Offset from audio start (milliseconds)
    duration_ms: float


async def generate_tts(
    script: str,
    output_dir: Path,
    voice: str = _DEFAULT_VOICE,
    rate: str = _DEFAULT_RATE,
    max_duration_sec: float = _MAX_DURATION_SEC,
    provider: str = "edge-tts",
    elevenlabs_voice_id: str | None = None,
) -> TTSResult:
    """Generate TTS audio and word-boundary timing from *script*.

    Args:
        script: The text to synthesise.
        output_dir: Directory to write `voiceover.mp3` and `word_boundaries.json`.
        voice: edge-tts voice name (see `edge-tts --list-voices`).
        rate: Speech rate modifier e.g. "+5%" or "-5%".
        max_duration_sec: Reject audio longer than this many seconds.
        provider: "edge-tts" (free) or "elevenlabs" (Zack D Films style/clone).
        elevenlabs_voice_id: Voice ID if using ElevenLabs.

    Returns:
        A :class:`TTSResult` with paths and metadata.

    Raises:
        TTSError: On synthesis failure, empty output, or duration violations.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    audio_path = output_dir / "voiceover.mp3"
    wb_path = output_dir / "word_boundaries.json"

    eleven_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if provider.lower() == "elevenlabs" and eleven_key:
        v_id = elevenlabs_voice_id or "UgBBYS2sOqTuMpoF3BR0"
        logger.info("Generating TTS with ElevenLabs (voice_id=%s, words=%d)", v_id, len(script.split()))
        try:
            boundaries = await _synthesise_elevenlabs(script, audio_path, v_id, eleven_key)
        except TTSError as exc:
            logger.warning("ElevenLabs generation for voice %s failed (%s). Using your chosen voice: %s", v_id, exc, voice)
            boundaries = await _synthesise(script, audio_path, voice, rate)
    else:
        logger.info("Generating TTS with your specified voice: %s (rate=%s, words=%d)", voice, rate, len(script.split()))
        boundaries = await _synthesise(script, audio_path, voice, rate)

    # Validate audio file
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        raise TTSError(f"TTS produced an empty audio file: {audio_path}")

    # Save word boundaries
    wb_dicts = [
        {"word": wb.word, "start_ms": wb.start_ms, "duration_ms": wb.duration_ms}
        for wb in boundaries
    ]
    wb_path.write_text(json.dumps(wb_dicts, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.debug("Saved %d word boundaries to %s", len(boundaries), wb_path)

    # Estimate duration from last boundary or probe
    duration_sec = _estimate_duration(boundaries, audio_path)
    logger.info("TTS audio duration estimate: %.2f seconds", duration_sec)

    if duration_sec < _MIN_DURATION_SEC:
        raise TTSError(
            f"TTS audio is suspiciously short ({duration_sec:.1f}s). "
            "The script may be too short or the voice failed silently."
        )
    if duration_sec > max_duration_sec:
        raise TTSError(
            f"TTS audio is too long ({duration_sec:.1f}s > max {max_duration_sec}s). "
            "Shorten the script or increase max_video_duration_sec in config."
        )

    return TTSResult(
        audio_path=audio_path,
        word_boundary_path=wb_path,
        duration_sec=duration_sec,
        word_count=len(script.split()),
    )


def load_word_boundaries(wb_path: Path) -> list[WordBoundary]:
    """Load word boundaries from a JSON file written by :func:`generate_tts`."""
    data = json.loads(Path(wb_path).read_text(encoding="utf-8"))
    return [
        WordBoundary(
            word=item["word"],
            start_ms=float(item["start_ms"]),
            duration_ms=float(item["duration_ms"]),
        )
        for item in data
    ]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _synthesise_elevenlabs(
    script: str,
    audio_path: Path,
    voice_id: str,
    api_key: str,
) -> list[WordBoundary]:
    """Generate audio and word-level timestamps using ElevenLabs with-timestamps API."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "text": script,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.85,
        },
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise TTSError(f"ElevenLabs TTS failed ({resp.status_code}): {resp.text}")
        data = resp.json()

    audio_b64 = data.get("audio_base64")
    if not audio_b64:
        raise TTSError("ElevenLabs returned empty audio")

    audio_path.write_bytes(base64.b64decode(audio_b64))

    # Parse character-level alignment into words
    alignment = data.get("alignment", {})
    characters = alignment.get("characters", [])
    start_times = alignment.get("character_start_times_seconds", [])
    end_times = alignment.get("character_end_times_seconds", [])

    boundaries: list[WordBoundary] = []
    cur_chars: list[str] = []
    cur_start: float | None = None
    cur_end: float | None = None

    for char, start_s, end_s in zip(characters, start_times, end_times):
        if char.isspace():
            if cur_chars and cur_start is not None and cur_end is not None:
                word = "".join(cur_chars)
                start_ms = cur_start * 1000.0
                dur_ms = max(50.0, (cur_end - cur_start) * 1000.0)
                boundaries.append(WordBoundary(word=word, start_ms=start_ms, duration_ms=dur_ms))
                cur_chars = []
                cur_start = None
        else:
            if cur_start is None:
                cur_start = start_s
            cur_end = end_s
            cur_chars.append(char)

    if cur_chars and cur_start is not None and cur_end is not None:
        word = "".join(cur_chars)
        start_ms = cur_start * 1000.0
        dur_ms = max(50.0, (cur_end - cur_start) * 1000.0)
        boundaries.append(WordBoundary(word=word, start_ms=start_ms, duration_ms=dur_ms))

    return boundaries


@retry_with_backoff(
    exceptions=(EdgeTTSException, asyncio.TimeoutError, ConnectionError),
    max_attempts=3,
    wait_min=2.0,
)
async def _synthesise(
    script: str,
    audio_path: Path,
    voice: str,
    rate: str,
) -> list[WordBoundary]:
    """Call edge-tts, save MP3, and collect WordBoundary events."""
    communicate = Communicate(text=script, voice=voice, rate=rate)
    boundaries: list[WordBoundary] = []

    audio_chunks: list[bytes] = []
    async for chunk in communicate.stream():
        chunk_type = chunk.get("type")
        if chunk_type == "audio":
            data = chunk.get("data")
            if data:
                audio_chunks.append(data)
        elif chunk_type == "WordBoundary":
            word = chunk.get("text", "")
            offset_ticks = chunk.get("offset", 0)
            duration_ticks = chunk.get("duration", 0)
            boundaries.append(
                WordBoundary(
                    word=word,
                    start_ms=offset_ticks / 10_000,
                    duration_ms=duration_ticks / 10_000,
                )
            )
        elif chunk_type == "SentenceBoundary":
            text = chunk.get("text", "")
            offset_ticks = chunk.get("offset", 0)
            duration_ticks = chunk.get("duration", 0)
            start_ms = offset_ticks / 10_000
            duration_ms = duration_ticks / 10_000
            words = text.split()
            if words:
                tot_chars = max(1, sum(len(w) for w in words))
                cur = start_ms
                for w in words:
                    wdur = (len(w) / tot_chars) * duration_ms
                    boundaries.append(
                        WordBoundary(
                            word=w,
                            start_ms=cur,
                            duration_ms=wdur,
                        )
                    )
                    cur += wdur

    if not audio_chunks:
        raise TTSError("edge-tts returned no audio data")

    audio_path.write_bytes(b"".join(audio_chunks))
    return boundaries


def _probe_audio_duration(audio_path: Path) -> float:
    """Read actual duration from MP3 file using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception:
        return 0.0


def _estimate_duration(boundaries: list[WordBoundary], audio_path: Path | None = None) -> float:
    """Estimate audio duration (seconds) from word boundaries or ffprobe."""
    if boundaries:
        last = boundaries[-1]
        calc = (last.start_ms + last.duration_ms) / 1000.0
        if calc > 0.5:
            return calc
    if audio_path and audio_path.exists():
        dur = _probe_audio_duration(audio_path)
        if dur > 0.0:
            return dur
    return 0.0


if __name__ == "__main__":
    import asyncio
    from utils.logging_config import setup_logging

    setup_logging()

    _SAMPLE_SCRIPT = (
        "Picture this. You're at work, minding your own business, when suddenly "
        "your coworker walks in wearing the exact same outfit as you. Not similar. "
        "Identical. Down to the socks. Turns out you both bought the exact same "
        "sale item from the same website on the same day. The worst part? "
        "Your boss assumed you planned it as a team bonding thing and wants you "
        "to do it every Friday now."
    )

    async def _test() -> None:
        result = await generate_tts(_SAMPLE_SCRIPT, Path("tmp/tts_test"))
        print(f"\n✅ TTS OK:")
        print(f"   Audio:    {result.audio_path}")
        print(f"   Duration: {result.duration_sec:.2f}s")
        print(f"   Words:    {result.word_count}")
        wbs = load_word_boundaries(result.word_boundary_path)
        print(f"   Boundaries: {len(wbs)} words")
        for wb in wbs[:5]:
            print(f"     [{wb.start_ms:.0f}ms] {wb.word!r} ({wb.duration_ms:.0f}ms)")

    asyncio.run(_test())
