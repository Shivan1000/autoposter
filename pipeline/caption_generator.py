"""
pipeline/caption_generator.py

Generate a punchy Instagram caption + hashtags for a Reel using Google Gemini (free).

Kept as a separate module from script_writer.py so each can be tested,
retried, and configured independently.
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass

import google.generativeai as genai
from dotenv import load_dotenv

from pipeline.reddit_scraper import RedditPost
from utils.retry import retry_with_backoff

load_dotenv()
logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-flash-latest"

_SYSTEM_PROMPT = """\
You are a top-tier viral Instagram Reels content strategist in 2026.
Your captions are engineered to stop the scroll, maximize watch time, and drive huge comment engagement.

Structure your caption in 3 parts:
1. THE VIRAL HOOK (First line): A bold, curious, or shocking one-liner (e.g. "Wait until you see how this ended... 💀👇", "Bro thought nobody would notice 😭💀", "This sounds completely unreal until you realize it's 100% true 👇").
2. THE HIGHLIGHT (1-2 sentences): A hilarious, punchy summary of the wildest part of the story.
3. ENGAGEMENT CTA (Final line): A direct, conversation-starting question (e.g. "What would you have done in this situation? Drop your thoughts below 👇💬" or "Tag a friend who would definitely do this 😭👇").
4. HASHTAGS: 6-8 trending high-volume hashtags combining viral tags with niche tags (e.g. #trending #viralreels #redditstories #reddit #funnyreels #storytime #fyp #explorepage).

Format your response strictly as:
CAPTION: <the complete caption text with hook, highlight, and CTA>
HASHTAGS: #tag1 #tag2 #tag3 #tag4 #tag5 #tag6 #tag7 #tag8"""

_USER_PROMPT_TEMPLATE = """\
Tone: {tone}

Reddit post title: {title}

Script summary:
{script_excerpt}

Generate the viral trendy Instagram caption and hashtags now:"""

_TONE_OPENERS = {
    "funny": "🤣",
    "hilarious": "💀",
    "humorous": "😂",
    "unhinged_funny": "😭💀",
    "sarcastic": "🙃",
    "dramatic": "😱",
    "curious": "🤔",
    "shocked": "😳",
    "unhinged": "💀",
}


@dataclass
class Caption:
    """Generated Instagram caption + hashtags."""
    text: str            # Caption body (no hashtags)
    hashtags: list[str]  # List of hashtag strings (with #)
    tone: str
    title: str = ""

    @property
    def full_caption(self) -> str:
        """Combined title + caption + hashtags ready for Instagram."""
        tags = " ".join(self.hashtags)
        parts = []
        if self.title:
            parts.append(f"📌 {self.title}")
        if self.text:
            parts.append(self.text)
        if tags:
            parts.append(tags)
        return "\n\n".join(parts)


class CaptionGeneratorError(RuntimeError):
    """Raised when caption generation fails."""


class CaptionGenerator:
    """Generate Instagram captions via Google Gemini.

    Args:
        model: Gemini model identifier.
        max_tokens: Max tokens for response.
        temperature: Sampling temperature.
        tone_presets: List of available tones.
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        max_tokens: int = 1024,
        temperature: float = 1.0,
        tone_presets: list[str] | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.tone_presets = tone_presets or ["funny", "dramatic", "curious", "shocked", "unhinged"]
        self._configure_client()

    async def generate(
        self,
        post: RedditPost,
        script: str,
        tone: str | None = None,
    ) -> Caption:
        """Generate a caption for *post*.

        Args:
            post: The source Reddit post.
            script: The voiceover script (used as context for the caption).
            tone: Override tone. If None, randomly chosen from presets.

        Returns:
            A :class:`Caption` dataclass.

        Raises:
            CaptionGeneratorError: On API failure or parse error.
        """
        chosen_tone = tone or random.choice(self.tone_presets)
        logger.info(
            "Generating caption for post [%s] with tone=%s", post.id, chosen_tone
        )

        raw = await self._call_api(post.title, script, chosen_tone)
        caption = self._parse_response(raw, chosen_tone)
        caption.title = post.title

        logger.info(
            "Caption generated (%d chars, %d hashtags)",
            len(caption.text), len(caption.hashtags),
        )
        return caption

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @retry_with_backoff(
        exceptions=(Exception,),
        max_attempts=3,
        wait_min=2.0,
    )
    async def _call_api(self, title: str, script: str, tone: str) -> str:
        """Call Gemini and return the raw text response."""
        script_excerpt = script[:300] + ("…" if len(script) > 300 else "")
        user_msg = _USER_PROMPT_TEMPLATE.format(
            tone=tone, title=title, script_excerpt=script_excerpt
        )

        models_to_try = [self.model]
        for fallback in ["gemini-3.5-flash", "gemini-3.1-flash-lite"]:
            if fallback not in models_to_try:
                models_to_try.append(fallback)

        last_exc = None
        for m_name in models_to_try:
            try:
                model = genai.GenerativeModel(
                    model_name=m_name,
                    system_instruction=_SYSTEM_PROMPT,
                    generation_config=genai.GenerationConfig(
                        max_output_tokens=self.max_tokens,
                        temperature=self.temperature,
                    ),
                )

                response = model.generate_content(user_msg)

                if not response.text:
                    raise CaptionGeneratorError("Gemini returned an empty caption response")

                return response.text.strip()

            except Exception as exc:
                last_exc = exc
                error_msg = str(exc)
                if "API_KEY" in error_msg or "403" in error_msg or "401" in error_msg:
                    raise CaptionGeneratorError(
                        f"Gemini API authentication failed — check GEMINI_API_KEY: {exc}"
                    ) from exc
                if "429" in error_msg or "ResourceExhausted" in error_msg or "quota" in error_msg.lower():
                    logger.warning("Caption model %s hit rate limit, trying fallback...", m_name)
                    continue
                raise

        if last_exc:
            raise last_exc

    def _parse_response(self, raw: str, tone: str) -> Caption:
        """Parse the structured response into a Caption dataclass."""
        lines = {
            line.split(":", 1)[0].strip().upper(): line.split(":", 1)[1].strip()
            for line in raw.splitlines()
            if ":" in line
        }

        caption_text = lines.get("CAPTION", "")
        hashtags_str = lines.get("HASHTAGS", "")

        if not caption_text:
            raise CaptionGeneratorError(
                f"Could not parse CAPTION from Gemini response:\n{raw}"
            )

        # Add tone emoji prefix if not already present
        emoji = _TONE_OPENERS.get(tone, "")
        if emoji and not caption_text.startswith(emoji):
            caption_text = f"{emoji} {caption_text}"

        # Parse hashtags
        hashtags = [
            word if word.startswith("#") else f"#{word}"
            for word in hashtags_str.split()
            if word
        ]

        # Fallback hashtags if none were parsed
        if not hashtags:
            logger.warning("No hashtags parsed from response — using defaults")
            hashtags = ["#reddit", "#reels", "#viral", "#storytime"]

        return Caption(text=caption_text, hashtags=hashtags, tone=tone)

    @staticmethod
    def _configure_client() -> None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY must be set in .env")
        genai.configure(api_key=api_key)


if __name__ == "__main__":
    import asyncio
    from utils.logging_config import setup_logging

    setup_logging()

    async def _test() -> None:
        post = RedditPost(
            id="test456",
            title="AITA for telling my sister her wedding dress looks like a tablecloth?",
            permalink="/r/AmItheAsshole/comments/test456/",
            url="https://www.reddit.com/r/AmItheAsshole/comments/test456/",
            subreddit="AmItheAsshole",
            score=32000,
            top_comments=["NTA, honesty is kindness.", "YTA that's her special day!"],
        )
        script = (
            "My sister spent three months picking her wedding dress. "
            "When she finally showed me, I panicked and said the first thing "
            "that came to mind. Now she won't return my calls."
        )
        gen = CaptionGenerator()
        caption = await gen.generate(post, script)
        print(f"\nTone: {caption.tone}")
        print(f"Caption: {caption.text}")
        print(f"Hashtags: {' '.join(caption.hashtags)}")
        print(f"\nFull:\n{caption.full_caption}")

    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    asyncio.run(_test())
