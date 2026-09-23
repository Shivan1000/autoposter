"""
pipeline/script_writer.py

Generate a short spoken script from a Reddit post using Google Gemini (free).

The script should:
  - Sound natural when read aloud (~30–45 seconds of speech)
  - Match the requested tone (funny / dramatic / curious / shocked / unhinged)
  - Stay within the configured word count ceiling
  - NOT be a verbatim copy of the post — it should be a punchy retelling
"""

from __future__ import annotations

import logging
import os

import google.generativeai as genai
from dotenv import load_dotenv

from pipeline.reddit_scraper import RedditPost
from utils.retry import retry_with_backoff

load_dotenv()
logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-flash-latest"
_DEFAULT_MAX_WORDS = 130

_SYSTEM_PROMPT = """\
You are a viral short-form video scriptwriter for Reddit Reels and TikToks.
Your video follows this exact 2-part structure:
1. THE MAIN TITLE HOOK (First 1-2 sentences):
   Hook the viewer by stating the Reddit post's question or scenario in an intriguing, punchy way.
2. THE SINGLE COMMENT STORY (Remaining sentences):
   Tell the specific story or reply given in the top comment. Retell it vividly, humorously, or dramatically as a single coherent narrative.

Rules:
- You MUST focus strictly on the main title and this SINGLE comment story. Do NOT talk about multiple comments or give generic advice.
- Write ONLY the spoken words. No stage directions, no speaker tags (like "Narrator:"), no markdown, no quotes wrapping the text.
- Do NOT start with "Okay so", "Alright guys", or "So basically".
- Length MUST be between 65 and {max_words} words (aim for ~80–95 words).
- End with a punchy closer or funny takeaway."""

_USER_PROMPT_TEMPLATE = """\
Tone: {tone}

Reddit Post Title: {title}

Featured Single Comment:
{comments}

Write the viral voiceover script focusing on this title and this single comment story now:"""


class ScriptWriterError(RuntimeError):
    """Raised when script generation fails."""


class ScriptWriter:
    """Generate spoken voiceover scripts via Google Gemini.

    Args:
        model: Gemini model identifier.
        max_words: Target word count ceiling for the script.
        max_tokens: Maximum tokens for the response.
        temperature: Sampling temperature (higher = more creative).
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        max_words: int = _DEFAULT_MAX_WORDS,
        max_tokens: int = 2048,
        temperature: float = 1.0,
    ) -> None:
        self.model = model
        self.max_words = max_words
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._configure_client()

    async def generate(self, post: RedditPost, tone: str = "funny") -> str:
        """Generate a spoken script from *post*.

        Args:
            post: The Reddit post to script.
            tone: One of funny / dramatic / curious / shocked / unhinged.

        Returns:
            The raw script text (no headers, no markdown).

        Raises:
            ScriptWriterError: On API failure or if output is too short/long.
        """
        logger.info(
            "Generating script for post [%s] with tone=%s", post.id, tone
        )
        comments_text = self._format_comments(post.top_comments)
        script = await self._call_api(post.title, comments_text, tone)
        self._validate_script(script)
        logger.info(
            "Script generated: %d words", len(script.split())
        )
        return script

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @retry_with_backoff(
        exceptions=(Exception,),
        max_attempts=3,
        wait_min=2.0,
        wait_max=30.0,
    )
    async def _call_api(self, title: str, comments: str, tone: str) -> str:
        system = _SYSTEM_PROMPT.format(max_words=self.max_words)
        user_msg = _USER_PROMPT_TEMPLATE.format(
            tone=tone, title=title, comments=comments, max_words=self.max_words
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
                    system_instruction=system,
                    generation_config=genai.GenerationConfig(
                        max_output_tokens=self.max_tokens,
                        temperature=self.temperature,
                    ),
                )

                response = model.generate_content(user_msg)

                if not response.text:
                    raise ScriptWriterError("Gemini returned an empty response")

                return response.text.strip()

            except Exception as exc:
                last_exc = exc
                error_msg = str(exc)
                if "API_KEY" in error_msg or "403" in error_msg or "401" in error_msg:
                    raise ScriptWriterError(
                        f"Gemini API authentication failed — check GEMINI_API_KEY: {exc}"
                    ) from exc
                if "429" in error_msg or "ResourceExhausted" in error_msg or "quota" in error_msg.lower():
                    logger.warning("Model %s hit rate limit, trying fallback...", m_name)
                    continue
                raise

        if last_exc:
            raise last_exc

    def _validate_script(self, script: str) -> None:
        """Raise ScriptWriterError if the script is obviously malformed."""
        word_count = len(script.split())
        if word_count < 20:
            raise ScriptWriterError(
                f"Generated script is too short ({word_count} words). "
                "Gemini may have returned an error message instead of a script."
            )
        if word_count > self.max_words * 1.5:
            logger.warning(
                "Script is %d words (target ceiling: %d) — may produce long audio",
                word_count, self.max_words,
            )

    @staticmethod
    def _format_comments(comments: list[str]) -> str:
        if not comments:
            return "(no comments available)"
        # Select the single primary comment story
        return comments[0][:600]

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
            id="test123",
            title="TIFU by accidentally sending my boss a meme meant for my group chat",
            permalink="/r/tifu/comments/test123/",
            url="https://www.reddit.com/r/tifu/comments/test123/",
            subreddit="tifu",
            score=45000,
            top_comments=[
                "This is why I have separate contacts for work and personal.",
                "What was the meme? We need details.",
                "The good news is, your boss probably does the same thing.",
            ],
        )
        writer = ScriptWriter()
        script = await writer.generate(post, tone="funny")
        print("\nGenerated script:")
        print("-" * 60)
        print(script)
        print("-" * 60)
        print(f"Word count: {len(script.split())}")

    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    asyncio.run(_test())
