"""
pipeline/caption_generator.py

Generate a punchy Instagram caption + hashtags for a Reel using Google Gemini (free).

Kept as a separate module from script_writer.py so each can be tested,
retried, and configured independently.
"""

from __future__ import annotations

import asyncio
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

import httpx

_DEFAULT_MODEL = "gemini-3.5-flash-lite"

_SYSTEM_PROMPT = """\
You are an unhinged, hilarious viral Instagram Reels & TikTok content creator in 2026.
Your captions are raw, witty, relatable, and unapologetically 18+ uncensored (using natural profanity like "fuck", "bullshit", "lmao", "lol", "assholes", "shit", "bro what the fuck", "ain't no way", "this shit is wild" to make it funny and authentic).
Avoid sterile corporate speak. Write like a brutally honest, funny friend roasting the situation in a chaotic group chat.

Structure your caption in 3 parts:
1. THE VIRAL HOOK (First line): A bold, chaotic, scroll-stopping one-liner using uncensored humor/slang (e.g. "Ain't no fucking way this actually happened 😭💀👇", "Bro what the fuck did I just witness 💀", "This is the most relatable bullshit I've seen all week 👇").
2. THE HIGHLIGHT (1-2 sentences): A hilarious, unhinged reaction to the wildest part of the story with colorful profanity and comedic honesty.
3. ENGAGEMENT CTA (Final line): A blunt, funny question (e.g. "What kind of unhinged bullshit would you do here? Tell me in the comments 👇💬" or "Tag that one asshole who would definitely do this 💀👇").
4. HASHTAGS: 6-8 trending high-volume hashtags combining viral tags with niche tags (e.g. #trending #viralreels #redditstories #reddit #funnyreels #storytime #fyp #explorepage #meme).

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
    """Generate Instagram captions via 3-tier architecture:
    1. Google Gemini
    2. Groq AI (Llama / GPT-OSS)
    3. Pure Offline Fallback (Guaranteed never to crash)
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
        """Generate a caption using the 3-tier pipeline: Gemini -> Groq -> Offline."""
        chosen_tone = tone or random.choice(self.tone_presets)
        logger.info(
            "Generating caption for post [%s] with tone=%s", post.id, chosen_tone
        )

        # ------------------------------------------------------------------
        # Tier 1: Gemini
        # ------------------------------------------------------------------
        try:
            raw = await self._call_gemini(post.title, script, chosen_tone)
            caption = self._parse_response(raw, chosen_tone)
            caption.title = post.title
            logger.info("[Tier 1: Gemini] Caption generated (%d chars, %d hashtags)", len(caption.text), len(caption.hashtags))
            return caption
        except Exception as exc:
            logger.warning("[Tier 1: Gemini] Caption generation unavailable or hit limit (%s). Falling back to Tier 2: Groq...", exc)

        # ------------------------------------------------------------------
        # Tier 2: Groq
        # ------------------------------------------------------------------
        try:
            raw = await self._call_groq(post.title, script, chosen_tone)
            caption = self._parse_response(raw, chosen_tone)
            caption.title = post.title
            logger.info("[Tier 2: Groq] Caption generated (%d chars, %d hashtags)", len(caption.text), len(caption.hashtags))
            return caption
        except Exception as exc:
            logger.warning("[Tier 2: Groq] Caption generation unavailable (%s). Falling back to Tier 3: Pure Offline...", exc)

        # ------------------------------------------------------------------
        # Tier 3: Pure Offline Fallback (Guaranteed to never fail)
        # ------------------------------------------------------------------
        caption = self._offline_caption(post, chosen_tone)
        logger.info("[Tier 3: Pure Offline] Caption generated (%d chars, %d hashtags)", len(caption.text), len(caption.hashtags))
        return caption

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call_gemini(self, title: str, script: str, tone: str) -> str:
        """Call Gemini and return the raw text response."""
        script_excerpt = script[:300] + ("…" if len(script) > 300 else "")
        user_msg = _USER_PROMPT_TEMPLATE.format(
            tone=tone, title=title, script_excerpt=script_excerpt
        )

        models_to_try = [self.model]
        for fallback in ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-latest", "gemini-3.8-flash"]:
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

                safety_settings = {
                    genai.types.HarmCategory.HARM_CATEGORY_HARASSMENT: genai.types.HarmBlockThreshold.BLOCK_NONE,
                    genai.types.HarmCategory.HARM_CATEGORY_HATE_SPEECH: genai.types.HarmBlockThreshold.BLOCK_NONE,
                    genai.types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: genai.types.HarmBlockThreshold.BLOCK_NONE,
                    genai.types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: genai.types.HarmBlockThreshold.BLOCK_NONE,
                }

                response = await asyncio.to_thread(
                    model.generate_content,
                    user_msg,
                    safety_settings=safety_settings,
                )

                if not response.text:
                    raise CaptionGeneratorError("Gemini returned an empty caption response")

                return response.text.strip()

            except Exception as exc:
                last_exc = exc
                error_msg = str(exc)
                if (
                    "429" in error_msg
                    or "ResourceExhausted" in error_msg
                    or "quota" in error_msg.lower()
                    or "404" in error_msg
                    or "NotFound" in error_msg
                    or "not available" in error_msg.lower()
                    or "deprecated" in error_msg.lower()
                ):
                    logger.warning("Gemini caption model %s unavailable (%s), trying next Gemini model...", m_name, exc)
                    continue
                raise

        if last_exc:
            raise last_exc
        raise CaptionGeneratorError("All Gemini caption models failed")

    async def _call_groq(self, title: str, script: str, tone: str) -> str:
        """Call Groq API for caption generation."""
        groq_key = os.getenv("GROQ_API_KEY")
        if not groq_key:
            raise CaptionGeneratorError("GROQ_API_KEY not set in .env")

        script_excerpt = script[:300] + ("…" if len(script) > 300 else "")
        user_msg = _USER_PROMPT_TEMPLATE.format(
            tone=tone, title=title, script_excerpt=script_excerpt
        )

        groq_models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
        last_exc = None

        async with httpx.AsyncClient(timeout=30.0) as client:
            for g_model in groq_models:
                try:
                    resp = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {groq_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": g_model,
                            "messages": [
                                {"role": "system", "content": _SYSTEM_PROMPT},
                                {"role": "user", "content": user_msg},
                            ],
                            "temperature": self.temperature,
                            "max_tokens": self.max_tokens,
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        content = data["choices"][0]["message"]["content"]
                        if content and content.strip():
                            return content.strip()
                    else:
                        logger.warning("Groq model %s returned HTTP %s: %s", g_model, resp.status_code, resp.text)
                except Exception as exc:
                    last_exc = exc
                    logger.warning("Groq caption model %s failed: %s", g_model, exc)

        if last_exc:
            raise last_exc
        raise CaptionGeneratorError("All Groq caption models failed")

    def _offline_caption(self, post: RedditPost, tone: str) -> Caption:
        """Pure Offline Fallback: Use post title + high-engagement prompt + curated viral hashtags."""
        emoji = _TONE_OPENERS.get(tone, "💀")
        text = f"{emoji} {post.title}\n\nWhat kind of absolute bullshit is this lmao 😭💀 Tag that one asshole who would definitely do this 👇💬"
        hashtags = ["#memes", "#reels", "#viral", "#explore", "#reddit", "#funny", "#trending", "#fyp"]
        return Caption(text=text, hashtags=hashtags, tone=tone, title=post.title)

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
            # If structure was loose, use raw text directly as caption body
            clean_raw = raw.replace("CAPTION:", "").replace("HASHTAGS:", "").strip()
            caption_text = clean_raw if clean_raw else "Check out this wild story! 👇"

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

        if not hashtags:
            hashtags = ["#memes", "#reels", "#viral", "#explore", "#reddit", "#funny"]

        return Caption(text=caption_text, hashtags=hashtags, tone=tone)

    @staticmethod
    def _configure_client() -> None:
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            try:
                genai.configure(api_key=api_key)
            except Exception as e:
                logger.warning("Could not configure Gemini client: %s", e)
        else:
            logger.warning("GEMINI_API_KEY not configured in .env")


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
