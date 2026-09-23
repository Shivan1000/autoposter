"""
bot/discord_bot.py

Discord bot entrypoint with slash commands.

Commands:
  /generate subreddit:<name> [tone:<funny|dramatic|curious|shocked|unhinged>]
    → Triggers the full pipeline for the given subreddit
    → Sends live progress updates + final result embed to the channel

  /history [limit:<n>]
    → Shows the last N successfully posted Reels from the dedup store

  /status
    → Shows bot uptime, scheduled job status, and token health

Requires:
  - DISCORD_BOT_TOKEN in .env
  - discord.py 2.x with app_commands (slash commands) enabled
  - Bot invited with: bot + applications.commands scopes
  - Message Content Intent enabled in Discord Developer Portal
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from pipeline.dedup_store import DedupStore
from pipeline.orchestrator import PipelineResult, run_pipeline
from utils.logging_config import setup_logging

load_dotenv()
logger = logging.getLogger(__name__)

_VALID_TONES = ["funny", "dramatic", "curious", "shocked", "unhinged"]
_EMBED_COLOR_SUCCESS = discord.Color.from_rgb(0, 200, 100)
_EMBED_COLOR_FAILURE = discord.Color.from_rgb(220, 50, 50)
_EMBED_COLOR_INFO = discord.Color.from_rgb(88, 101, 242)  # Discord blurple

_START_TIME = datetime.now(timezone.utc)


import random
import uuid
from pathlib import Path
from pipeline.reddit_scraper import RedditScraper
from pipeline.script_writer import ScriptWriter
from pipeline.caption_generator import CaptionGenerator

class ThreadPreviewView(discord.ui.View):
    """Interactive view for generated Reddit thread previews."""
    def __init__(self, subreddit: str, tone: str, post_title: str):
        super().__init__(timeout=600)
        self.subreddit = subreddit
        self.tone = tone
        self.post_title = post_title

    @discord.ui.button(label="🎬 Post Reel to Instagram", style=discord.ButtonStyle.success, emoji="🚀")
    async def post_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        await interaction.followup.send(
            embed=_build_progress_embed(self.subreddit, self.tone, stage="🚀 Creating 40+ second Reel & publishing to Instagram…"),
            ephemeral=False,
        )
        asyncio.create_task(
            _run_pipeline_and_report(interaction, self.subreddit, self.tone, dry_run=False, is_followup=True)
        )

    @discord.ui.button(label="🎲 Another Thread", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def another_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass
        await _generate_and_send_thread(interaction, subreddit=None, tone=self.tone, is_followup=True)


async def _generate_and_send_thread(
    interaction_or_ctx,
    subreddit: str | None = None,
    tone: str | None = None,
    is_followup: bool = False,
) -> None:
    """Scrape a random Reddit thread, capture its screenshot, generate script preview, and send to Discord."""
    meme_subs = ["memes", "dankmemes", "me_irl", "wholesomememes", "funny", "comedyheaven", "AdviceAnimals", "MemeEconomy"]
    chosen_sub = subreddit.strip().lstrip("r/").lower() if (subreddit and subreddit.lower() != "random") else random.choice(meme_subs)
    chosen_tone = tone or "funny"

    # Progress message
    loading_embed = discord.Embed(
        title=f"🔍 Fetching Random Thread from r/{chosen_sub}...",
        description="⏳ Scraping post, capturing image card, and writing AI voiceover preview…",
        color=_EMBED_COLOR_INFO,
    )

    msg = None
    if hasattr(interaction_or_ctx, "response"):
        if is_followup:
            msg = await interaction_or_ctx.followup.send(embed=loading_embed)
        elif not interaction_or_ctx.response.is_done():
            await interaction_or_ctx.response.send_message(embed=loading_embed)
    else:
        msg = await interaction_or_ctx.send(embed=loading_embed)

    try:
        scraper = RedditScraper(time_filter="day", candidate_pool=20, allow_nsfw=False, require_images=True)
        temp_dir = Path("tmp") / "bot_threads"
        temp_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = temp_dir / f"card_{uuid.uuid4().hex[:8]}.png"

        post = await scraper.fetch_and_screenshot(chosen_sub, screenshot_path, bot.dedup_store)

        # Generate script preview (strictly focused on meme title and visual)
        script_writer = ScriptWriter()
        script = await script_writer.generate(post, tone=chosen_tone)
        words = len(script.split())
        est_sec = round(words / 2.4, 1)

        # Generate caption preview
        cap_gen = CaptionGenerator()
        caption_obj = await cap_gen.generate(post, script, tone=chosen_tone)

        # Build Rich Discord Embed
        embed = discord.Embed(
            title=f"📌 r/{chosen_sub} — {post.title[:100]}",
            url=post.url,
            description=f"**Author**: u/{post.author or 'reddit_user'} • **Score**: {post.score:,}\n🔗 [Open Reddit Thread]({post.url})",
            color=discord.Color.from_rgb(255, 69, 0),  # Reddit Orange
        )

        # Attach Screenshot (Clean meme card without comments)
        file = discord.File(str(screenshot_path.resolve()), filename="card.png")
        embed.set_image(url="attachment://card.png")

        # Voiceover Preview
        embed.add_field(
            name=f"🎙️ AI Narration Script ({words} words • ~{est_sec}s Reel)",
            value=f"```\n{script[:600]}\n```",
            inline=False,
        )

        # Instagram Caption Preview
        embed.add_field(
            name="📱 Viral Instagram Caption & Tags",
            value=f"{caption_obj.text[:300]}\n\n*{' '.join(caption_obj.hashtags[:6])}*",
            inline=False,
        )

        embed.set_footer(text="Click 'Post Reel to Instagram' below to automatically create and publish this Reel!")

        view = ThreadPreviewView(chosen_sub, chosen_tone, post.title)

        if hasattr(interaction_or_ctx, "response"):
            if is_followup:
                await interaction_or_ctx.followup.send(embed=embed, file=file, view=view)
            else:
                await interaction_or_ctx.edit_original_response(embed=embed, attachments=[file], view=view)
        else:
            if msg:
                await msg.delete()
            await interaction_or_ctx.send(embed=embed, file=file, view=view)

    except Exception as exc:
        logger.exception("Error generating random thread: %s", exc)
        err_embed = discord.Embed(
            title="❌ Could Not Fetch Thread",
            description=f"Failed to fetch from r/{chosen_sub}:\n```{exc}```",
            color=_EMBED_COLOR_FAILURE,
        )
        if hasattr(interaction_or_ctx, "response"):
            if is_followup:
                await interaction_or_ctx.followup.send(embed=err_embed)
            else:
                await interaction_or_ctx.edit_original_response(embed=err_embed)
        else:
            await interaction_or_ctx.send(embed=err_embed)


class AutoposterBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix=["!", "?"], intents=intents)
        self.dedup_store = DedupStore()

    async def setup_hook(self) -> None:
        await self.dedup_store.init()
        await self.tree.sync()
        logger.info("Slash commands synced to Discord")


bot = AutoposterBot()


# ---------------------------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="thread",
    description="🎲 Generate a random Reddit thread with screenshot card, script & caption preview",
)
@app_commands.describe(
    subreddit="Subreddit name (default: random funny subreddit, or specify e.g. funny, tifu, facepalm)",
    tone="Tone style (default: funny)",
)
@app_commands.choices(tone=[
    app_commands.Choice(name=t, value=t) for t in _VALID_TONES
])
async def thread_command(
    interaction: discord.Interaction,
    subreddit: str | None = None,
    tone: app_commands.Choice[str] | None = None,
) -> None:
    """Generate and send a random Reddit thread preview to Discord."""
    tone_value = tone.value if tone else "funny"
    await _generate_and_send_thread(interaction, subreddit=subreddit, tone=tone_value)


@bot.tree.command(
    name="random",
    description="🎲 Pick a random trending Reddit thread and preview it in Discord",
)
@app_commands.describe(
    subreddit="Subreddit name (default: random funny subreddit)",
)
async def random_command(
    interaction: discord.Interaction,
    subreddit: str | None = None,
) -> None:
    """Shortcut to get a random thread preview."""
    await _generate_and_send_thread(interaction, subreddit=subreddit, tone="funny")


@bot.tree.command(
    name="post",
    description="Generate and post a fresh Reel to Instagram (default: r/memes)",
)
@app_commands.describe(
    subreddit="Subreddit name (default: memes, or dankmemes, me_irl, comedyheaven)",
    tone="Script/caption tone style (default: funny)",
)
@app_commands.choices(tone=[
    app_commands.Choice(name=t, value=t) for t in _VALID_TONES
])
async def post_command(
    interaction: discord.Interaction,
    subreddit: str = "memes",
    tone: app_commands.Choice[str] | None = None,
) -> None:
    """Slash command: post a Reel to Instagram."""
    subreddit = subreddit.strip().lstrip("r/").lower()
    tone_value = tone.value if tone else "funny"

    # defer() shows Discord's "thinking..." indicator and keeps the token alive
    await interaction.response.defer(ephemeral=False)
    await interaction.followup.send(
        embed=_build_progress_embed(subreddit, tone_value, stage="🚀 Starting Reel generation pipeline…"),
    )

    asyncio.create_task(
        _run_pipeline_and_report(interaction, subreddit, tone_value, dry_run=False)
    )


@bot.tree.command(
    name="dryrun",
    description="Generate a video without uploading to Instagram (test run)",
)
@app_commands.describe(
    subreddit="Subreddit name (default: memes)",
    tone="Script/caption tone style",
)
@app_commands.choices(tone=[
    app_commands.Choice(name=t, value=t) for t in _VALID_TONES
])
async def dryrun_command(
    interaction: discord.Interaction,
    subreddit: str = "memes",
    tone: app_commands.Choice[str] | None = None,
) -> None:
    """Slash command: test render video without publishing."""
    subreddit = subreddit.strip().lstrip("r/").lower()
    tone_value = tone.value if tone else "funny"

    await interaction.response.defer(ephemeral=False)
    await interaction.followup.send(
        embed=_build_progress_embed(subreddit, tone_value, stage="🧪 Rendering test Reel (dry-run)…"),
    )

    asyncio.create_task(
        _run_pipeline_and_report(interaction, subreddit, tone_value, dry_run=True)
    )


@bot.tree.command(
    name="generate",
    description="Generate and post a Reel from a Reddit subreddit",
)
@app_commands.describe(
    subreddit="Subreddit name (without r/), e.g. memes, dankmemes",
    tone="Script/caption tone style",
)
@app_commands.choices(tone=[
    app_commands.Choice(name=t, value=t) for t in _VALID_TONES
])
async def generate(
    interaction: discord.Interaction,
    subreddit: str = "memes",
    tone: app_commands.Choice[str] | None = None,
) -> None:
    """Slash command: trigger the full Reddit → Instagram Reels pipeline."""
    subreddit = subreddit.strip().lstrip("r/").lower()
    if not subreddit.replace("_", "").isalnum():
        await interaction.response.send_message(
            "❌ Invalid subreddit name. Use only letters, numbers, and underscores.",
            ephemeral=True,
        )
        return

    tone_value = tone.value if tone else None

    # defer() immediately — pipeline takes time
    await interaction.response.defer(ephemeral=False)
    await interaction.followup.send(
        embed=_build_progress_embed(subreddit, tone_value, stage="Starting…"),
    )

    # Run pipeline in background
    asyncio.create_task(
        _run_pipeline_and_report(interaction, subreddit, tone_value, dry_run=False)
    )


# ---------------------------------------------------------------------------
# Text Prefix Commands (!thread, ?thread, !post, ?post, !random)
# ---------------------------------------------------------------------------

@bot.command(name="thread", aliases=["random", "getthread"])
async def prefix_thread(ctx: commands.Context, subreddit: str = "random") -> None:
    """Text command: !thread or ?thread [subreddit]"""
    await _generate_and_send_thread(ctx, subreddit=subreddit, tone="funny")


@bot.command(name="post")
async def prefix_post(ctx: commands.Context, subreddit: str = "memes") -> None:
    """Text command: !post or ?post [subreddit]"""
    sub = subreddit.strip().lstrip("r/").lower()
    await ctx.send(f"🚀 Starting Reel generation for **r/{sub}** and posting to Instagram...")
    result = await run_pipeline(sub, tone="funny", dry_run=False)
    embed = _build_result_embed(result)
    await ctx.send(embed=embed)


async def _run_pipeline_and_report(
    interaction: discord.Interaction,
    subreddit: str,
    tone: str | None,
    dry_run: bool = False,
    is_followup: bool = True,  # Always followup now (commands use defer+followup)
) -> None:
    """Run the pipeline and send the result back via followup (works after defer)."""
    try:
        result = await run_pipeline(subreddit, tone=tone, dry_run=dry_run)
        embed = _build_result_embed(result)
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        logger.exception("Unhandled error in pipeline task")
        error_embed = discord.Embed(
            title="💥 Pipeline Failed",
            description=f"Something went wrong:\n```{exc}```",
            color=_EMBED_COLOR_FAILURE,
        )
        try:
            await interaction.followup.send(embed=error_embed)
        except Exception as inner:
            logger.warning("Could not send error followup to Discord: %s", inner)


# ---------------------------------------------------------------------------
# /history command
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="history",
    description="Show recently posted Reels",
)
@app_commands.describe(limit="Number of recent posts to show (default 5, max 20)")
async def history(
    interaction: discord.Interaction,
    limit: int = 5,
) -> None:
    limit = max(1, min(limit, 20))
    posts = await bot.dedup_store.get_recent(limit)

    if not posts:
        await interaction.response.send_message(
            "📭 No Reels have been posted yet.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"📋 Last {len(posts)} Posted Reels",
        color=_EMBED_COLOR_INFO,
    )
    for post in posts:
        title = (post.get("reddit_title") or "(no title)")[:50]
        reel_url = post.get("reel_url") or "N/A"
        posted_at = post.get("posted_at", "")[:10]
        embed.add_field(
            name=f"r/{post['subreddit']} — {posted_at}",
            value=f"**{title}**\n[View Reel]({reel_url})" if reel_url != "N/A" else f"**{title}**",
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /status command
# ---------------------------------------------------------------------------

@bot.tree.command(name="status", description="Show bot status and health")
async def status(interaction: discord.Interaction) -> None:
    uptime = datetime.now(timezone.utc) - _START_TIME
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes = remainder // 60

    total = await bot.dedup_store.count()
    embed = discord.Embed(
        title="🤖 Autoposter Status",
        color=_EMBED_COLOR_INFO,
    )
    embed.add_field(name="Uptime", value=f"{hours}h {minutes}m", inline=True)
    embed.add_field(name="Total Reels posted", value=str(total), inline=True)
    embed.add_field(
        name="Bot latency",
        value=f"{round(bot.latency * 1000)}ms",
        inline=True,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="Reddit for content 👀",
        )
    )


@bot.event
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    logger.error("Slash command error: %s", error)
    msg = "❌ An error occurred while running this command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def _build_progress_embed(subreddit: str, tone: str | None, stage: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎬 Generating Reel for r/{subreddit}",
        description=f"⏳ {stage}",
        color=_EMBED_COLOR_INFO,
    )
    if tone:
        embed.add_field(name="Tone", value=tone, inline=True)
    embed.set_footer(text="This may take 1–3 minutes…")
    return embed


def _build_result_embed(result: PipelineResult) -> discord.Embed:
    if result.success:
        ig_user = os.getenv("INSTAGRAM_USERNAME", "").strip()
        link = result.reel_url
        if not link or link.strip() == "https://www.instagram.com/":
            link = f"https://www.instagram.com/{ig_user}/" if ig_user else "https://www.instagram.com/"

        profile_display = f"[@{ig_user}]({link})" if ig_user else f"[View Profile]({link})"

        embed = discord.Embed(
            title="✅ Reel Posted Successfully!",
            description=(
                f"**r/{result.subreddit}** → Instagram Reels\n\n"
                f"📝 **Post**: {result.reddit_title or '(unknown)'}\n"
                f"👤 **Instagram Profile**: {profile_display}\n"
                f"🔗 **URL**: {link}"
            ),
            color=_EMBED_COLOR_SUCCESS,
        )
        embed.add_field(name="Job ID", value=result.job_id, inline=True)
    else:
        embed = discord.Embed(
            title="❌ Pipeline Failed",
            description=(
                f"**Failed stage**: `{result.failed_stage}`\n"
                f"**Error type**: `{result.error_type}`\n\n"
                f"```\n{result.error_message or 'Unknown error'}\n```"
            ),
            color=_EMBED_COLOR_FAILURE,
        )
        embed.add_field(name="Subreddit", value=f"r/{result.subreddit}", inline=True)
        embed.add_field(name="Job ID", value=result.job_id, inline=True)
        embed.set_footer(text="The post has NOT been marked as processed — you can retry.")

    return embed


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise EnvironmentError("DISCORD_BOT_TOKEN must be set in .env")
    logger.info("Starting Autoposter Discord bot…")
    bot.run(token)


if __name__ == "__main__":
    main()
