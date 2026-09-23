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
_DEFAULT_MAX_WORDS = 145

_SYSTEM_PROMPT = """\
You are an elite, unhinged viral comedian and voiceover scriptwriter for top-tier TikTok and Instagram Reels.
Your goal is to write a wild, hysterically funny, and dramatically exaggerated 40-50+ second spoken voiceover script based purely on the Reddit meme/post title on screen.

Style & Comedic Guidelines:
- USE HILARIOUS EXAGGERATION & HYPERBOLE: Blow the relatable situation completely out of proportion in the funniest way possible. Take the simple everyday dilemma or meme and describe it like an epic, chaotic disaster or a high-stakes psychological meltdown.
- FAST-PACED, WITTY & NEVER BORING: Keep every sentence bursting with sharp comedic commentary, ridiculous metaphors, and rapid-fire punchy energy.
- RELATABLE ESCALATION: Start with the core premise, then escalate the absurdity and chaos step by step until it reaches peak comedy.
- KILLER CLOSER: End on a sharp, unexpected punchline that leaves the audience laughing out loud and tagging their friends in the comments.

Strict Rules:
- Focus ONLY on the main meme title and visual topic. Do NOT talk about reddit comments, usernames, or comment replies.
- Strictly output ONLY the spoken words. No speaker tags (like "Narrator:"), no stage directions, no brackets, no quotes.
- Never start with generic filler like "Okay so", "Alright guys", "Welcome back", or "So basically".
- Length MUST be between 115 and {max_words} words (aim for ~120–135 words to guarantee a 40–50+ second video)."""

_USER_PROMPT_TEMPLATE = """\
Tone: {tone}
Subreddit: r/{subreddit}
Reddit Meme / Post Title: {title}

Write the hilarious, wildly exaggerated 40-50s voiceover script for this meme now:"""


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
        script = await self._call_api(post.title, tone, subreddit=post.subreddit)
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
    async def _call_api(self, title: str, tone: str, subreddit: str = "memes") -> str:
        system = _SYSTEM_PROMPT.format(max_words=self.max_words)
        user_msg = _USER_PROMPT_TEMPLATE.format(
            tone=tone, title=title, max_words=self.max_words, subreddit=subreddit
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
