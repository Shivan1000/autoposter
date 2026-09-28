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

import asyncio
import logging
import os

import google.generativeai as genai
from dotenv import load_dotenv

from pipeline.reddit_scraper import RedditPost
from utils.retry import retry_with_backoff

load_dotenv()
logger = logging.getLogger(__name__)

import httpx

_DEFAULT_MODEL = "gemini-3.5-flash-lite"
_DEFAULT_MAX_WORDS = 135

_SYSTEM_PROMPT = """\
You are an expert viral short-form scriptwriter and storyteller for TikTok, Instagram Reels, and YouTube Shorts.
Your mission is to transform a Reddit post (Title + Top Comments) into an entertaining, polished, and highly engaging spoken voiceover narration (45-60 seconds, 100-140 words) that is EXCELLENT for Text-To-Speech (TTS).

==================================================
CRITICAL CORE RULE: ABSOLUTE SOURCE FAITHFULNESS
==================================================
The Reddit title and top comments are your absolute SOURCE OF TRUTH.
The generated script MUST remain strictly about the exact subject, dilemma, observation, or frustration of the Reddit post from beginning to end.

STRICT TOPIC RELEVANCE:
- If the Reddit post is about attention span -> stay strictly about attention span.
- If the Reddit post is about work/bosses -> stay strictly about work/bosses.
- If the Reddit post is about gaming -> stay strictly about gaming.
- If the Reddit post is about dating -> stay strictly about dating.
- If the Reddit post is about college/school -> stay strictly about college/school.
- Never replace the subject with a different scenario simply because it makes the script easier to write.

NO UNRELATED INVENTED SCENARIOS:
- DO NOT invent random unrelated locations, jobs, restaurants, coffee shops, movies, streaming apps, brands, or conversations unless they are explicitly present in the Reddit content.
- For example, if the Reddit post is about zoning out while listening, DO NOT invent a story about browsing Netflix or standing in a coffee shop line.
- Creative observational humor and relatable hyperbole are allowed ONLY when directly reinforcing the original Reddit topic.

==================================================
HOW TO USE THE TITLE AND COMMENTS
==================================================
1. Understand the core topic: Together, the title and comments define what this post is actually about. If the title is vague, the comments define the exact context.
2. Synthesize the comments: Identify the funniest, most relatable observations and shared struggles from the comments. Weave those ideas into a seamless, unified spoken narrative.
3. NEVER use Reddit meta language: DO NOT say "According to Reddit", "The Reddit post says", "The comments say", "The OP", or "Reddit users are saying". The viewer should feel like they are hearing an entertaining story/commentary, not an AI reading Reddit metadata.

==================================================
SCRIPT STRUCTURE & PACING
==================================================
1. HOOK (First 1-2 sentences): Immediately establish the actual Reddit topic with a relatable, scroll-stopping observation or rhetorical question that grabs the listener.
2. SETUP & DEVELOPMENT: Unpack the relatable situation using the core ideas from the Reddit comments.
3. ESCALATION & HUMOR: Escalate the comedic absurdity or emotional truth of the situation with sharp observational humor.
4. PAYOFF: End on a strong, memorable closer or punchline that directly ties back to the central theme.

==================================================
TONE & LANGUAGE GUIDELINES
==================================================
- If TONE is "uncensored" or "unhinged":
  Unleash raw, hilarious, 18+ uncensored comedic language (using natural profanity like fuck, bullshit, shit, lmao, lol, assholes, wtf) to deliver an unhinged, laugh-out-loud funny narration, while remaining 100% faithful to the Reddit post's exact subject.
- If TONE is "funny" or "hilarious":
  Fast-paced, witty, relatable observational comedy with high energy.

==================================================
STRICT TTS & FORMATTING RULES
==================================================
- Word count: Strictly between 100 and 140 spoken words (aim for ~115-130 words for a 45-60 second read).
- Voice & Tone: Conversational, punchy, rhythmic. Use natural punctuation (commas, periods, dashes) to create natural speech pauses.
- Absolutely NO stage directions, speaker tags (like "Narrator:"), brackets, or parentheses.
- Absolutely NO emojis, NO markdown formatting (no asterisks or bold text), NO quotes wrapping the script.
- Absolutely NO generic filler or social media CTAs (DO NOT say "What do you think?", "Let me know in the comments", "Drop your thoughts below", "Follow for more").
- Output ONLY the spoken words to be read aloud by the voice engine."""

_USER_PROMPT_TEMPLATE = """\
SUBREDDIT: r/{subreddit}
TONE: {tone}
REDDIT TITLE:
{title}

TOP REDDIT COMMENTS:
{comments}

Write the polished, source-faithful 100-140 word spoken voiceover script now. Output ONLY the spoken text:"""


class ScriptWriterError(RuntimeError):
    """Raised when script generation fails."""


def _clean_script_output(text: str) -> str:
    """Strip formatting, markdown, quotes, or accidental prefixes for clean TTS."""
    cleaned = text.strip()
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
        cleaned = cleaned[1:-1].strip()
    cleaned = cleaned.replace("**", "").replace("*", "")
    for prefix in ["Script:", "Voiceover:", "Voiceover Script:", "Here is the script:"]:
        if cleaned.lower().startswith(prefix.lower()):
            cleaned = cleaned[len(prefix):].strip()
    return cleaned.strip()


class ScriptWriter:
    """Generate spoken voiceover scripts via 3-tier architecture:
    1. Google Gemini
    2. Groq AI (Llama / GPT-OSS)
    3. Pure Offline Fallback (Direct Reddit title + comments)
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        max_words: int = _DEFAULT_MAX_WORDS,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> None:
        self.model = model
        self.max_words = max_words
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._configure_client()

    async def generate(self, post: RedditPost, tone: str = "funny") -> str:
        """Generate a spoken script using the 3-tier pipeline: Gemini -> Groq -> Offline."""
        logger.info(
            "Generating script for post [%s] with tone=%s", post.id, tone
        )
        comments_str = self._format_comments(post.top_comments)

        # ------------------------------------------------------------------
        # Tier 1: Gemini
        # ------------------------------------------------------------------
        try:
            raw_script = await self._call_gemini(
                title=post.title,
                tone=tone,
                subreddit=post.subreddit,
                comments=comments_str,
            )
            script = _clean_script_output(raw_script)
            self._validate_script(script)
            logger.info("[Tier 1: Gemini] Script generated: %d words", len(script.split()))
            return script
        except Exception as exc:
            logger.warning("[Tier 1: Gemini] Unavailable or hit limit (%s). Falling back to Tier 2: Groq...", exc)

        # ------------------------------------------------------------------
        # Tier 2: Groq
        # ------------------------------------------------------------------
        try:
            raw_script = await self._call_groq(
                title=post.title,
                tone=tone,
                subreddit=post.subreddit,
                comments=comments_str,
            )
            script = _clean_script_output(raw_script)
            self._validate_script(script)
            logger.info("[Tier 2: Groq] Script generated: %d words", len(script.split()))
            return script
        except Exception as exc:
            logger.warning("[Tier 2: Groq] Unavailable or failed (%s). Falling back to Tier 3: Pure Offline...", exc)

        # ------------------------------------------------------------------
        # Tier 3: Pure Offline Fallback (Guaranteed to never crash)
        # ------------------------------------------------------------------
        script = self._offline_script(post)
        logger.info("[Tier 3: Pure Offline] Script generated: %d words", len(script.split()))
        return script

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call_gemini(
        self,
        title: str,
        tone: str,
        subreddit: str = "memes",
        comments: str = "",
    ) -> str:
        system = _SYSTEM_PROMPT
        user_msg = _USER_PROMPT_TEMPLATE.format(
            tone=tone,
            title=title,
            comments=comments,
            subreddit=subreddit,
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
                    system_instruction=system,
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
                    raise ScriptWriterError("Gemini returned an empty response")

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
                    logger.warning("Gemini model %s unavailable (%s), trying next Gemini model...", m_name, exc)
                    continue
                raise

        if last_exc:
            raise last_exc
        raise ScriptWriterError("All Gemini models exhausted")

    async def _call_groq(
        self,
        title: str,
        tone: str,
        subreddit: str = "memes",
        comments: str = "",
    ) -> str:
        """Call Groq API using high-performance open-source models."""
        groq_key = os.getenv("GROQ_API_KEY")
        if not groq_key:
            raise ScriptWriterError("GROQ_API_KEY not set in .env")

        system = _SYSTEM_PROMPT
        user_msg = _USER_PROMPT_TEMPLATE.format(
            tone=tone,
            title=title,
            comments=comments,
            subreddit=subreddit,
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
                                {"role": "system", "content": system},
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
                    logger.warning("Groq model %s failed: %s", g_model, exc)

        if last_exc:
            raise last_exc
        raise ScriptWriterError("All Groq models failed")

    def _offline_script(self, post: RedditPost) -> str:
        """Pure Offline Fallback: Read Reddit post title and comments directly without rewriting."""
        title = post.title.strip()
        if not title.endswith((".", "!", "?")):
            title += "."

        sentences = [title]
        if post.top_comments:
            for comment in post.top_comments[:2]:
                c_clean = " ".join(comment.split())
                if len(c_clean) > 160:
                    c_clean = c_clean[:157] + "..."
                if c_clean:
                    sentences.append(f"Top comment says: {c_clean}")

        sentences.append("What would you do in this situation? Let us know in the comments below!")
        text = " ".join(sentences)

        # Ensure reasonable length for spoken narration
        if len(text.split()) < 25:
            text += " This is genuinely hilarious and so relatable. Follow for more daily memes!"

        return text

    def _validate_script(self, script: str) -> None:
        """Raise ScriptWriterError if the script is obviously malformed."""
        word_count = len(script.split())
        if word_count < 15:
            raise ScriptWriterError(
                f"Generated script is too short ({word_count} words)."
            )
        if word_count > self.max_words * 1.5:
            logger.warning(
                "Script is %d words (target ceiling: %d) — may produce long audio",
                word_count, self.max_words,
            )

    @staticmethod
    def _format_comments(comments: list[str]) -> str:
        if not comments:
            return "(No comments provided. Rely strictly on the Reddit title topic.)"
        formatted = []
        for i, c in enumerate(comments[:6], 1):
            clean = " ".join(c.split())
            if clean:
                formatted.append(f"{i}. {clean}")
        return "\n".join(formatted) if formatted else "(No comments provided.)"

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
