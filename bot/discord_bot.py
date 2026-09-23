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


class AutoposterBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.dedup_store = DedupStore()

    async def setup_hook(self) -> None:
        await self.dedup_store.init()
        await self.tree.sync()
        logger.info("Slash commands synced to Discord")


bot = AutoposterBot()


# ---------------------------------------------------------------------------
# /generate command
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="post",
    description="Generate and post a fresh Reel to Instagram (default: r/funny)",
)
@app_commands.describe(
    subreddit="Subreddit name (default: funny, or tifu, facepalm, AskReddit)",
    tone="Script/caption tone style (default: funny)",
)
@app_commands.choices(tone=[
    app_commands.Choice(name=t, value=t) for t in _VALID_TONES
])
async def post_command(
    interaction: discord.Interaction,
    subreddit: str = "funny",
    tone: app_commands.Choice[str] | None = None,
) -> None:
    """Slash command: post a Reel to Instagram."""
    subreddit = subreddit.strip().lstrip("r/").lower()
    tone_value = tone.value if tone else "funny"

    await interaction.response.send_message(
        embed=_build_progress_embed(subreddit, tone_value, stage="🚀 Starting Reel generation pipeline…"),
        ephemeral=False,
    )

    asyncio.create_task(
        _run_pipeline_and_report(interaction, subreddit, tone_value, dry_run=False)
    )


@bot.tree.command(
    name="dryrun",
    description="Generate a video without uploading to Instagram (test run)",
)
@app_commands.describe(
    subreddit="Subreddit name (default: funny)",
    tone="Script/caption tone style",
)
@app_commands.choices(tone=[
    app_commands.Choice(name=t, value=t) for t in _VALID_TONES
])
async def dryrun_command(
    interaction: discord.Interaction,
    subreddit: str = "funny",
    tone: app_commands.Choice[str] | None = None,
) -> None:
    """Slash command: test render video without publishing."""
    subreddit = subreddit.strip().lstrip("r/").lower()
    tone_value = tone.value if tone else "funny"

    await interaction.response.send_message(
        embed=_build_progress_embed(subreddit, tone_value, stage="🧪 Rendering test Reel (dry-run)…"),
        ephemeral=False,
    )

    asyncio.create_task(
        _run_pipeline_and_report(interaction, subreddit, tone_value, dry_run=True)
    )


@bot.tree.command(
    name="generate",
    description="Generate and post a Reel from a Reddit subreddit",
)
@app_commands.describe(
    subreddit="Subreddit name (without r/), e.g. funny, AskReddit",
    tone="Script/caption tone style",
)
@app_commands.choices(tone=[
    app_commands.Choice(name=t, value=t) for t in _VALID_TONES
])
async def generate(
    interaction: discord.Interaction,
    subreddit: str = "funny",
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

    # Acknowledge immediately — pipeline takes time
    await interaction.response.send_message(
        embed=_build_progress_embed(subreddit, tone_value, stage="Starting…"),
        ephemeral=False,
    )

    # Run pipeline in background
    asyncio.create_task(
        _run_pipeline_and_report(interaction, subreddit, tone_value, dry_run=False)
    )


async def _run_pipeline_and_report(
    interaction: discord.Interaction,
    subreddit: str,
    tone: str | None,
    dry_run: bool = False,
) -> None:
    """Run the pipeline and update the Discord message with the result."""
    try:
        result = await run_pipeline(subreddit, tone=tone, dry_run=dry_run)
        embed = _build_result_embed(result)
        await interaction.edit_original_response(embed=embed)
    except Exception as exc:
        logger.exception("Unhandled error in pipeline task")
        error_embed = discord.Embed(
            title="💥 Critical Error",
            description=f"An unexpected error prevented the pipeline from running:\n```{exc}```",
            color=_EMBED_COLOR_FAILURE,
        )
        try:
            await interaction.edit_original_response(embed=error_embed)
        except Exception:
            pass


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
        embed = discord.Embed(
            title="✅ Reel Posted Successfully!",
            description=(
                f"**r/{result.subreddit}** → Instagram Reels\n\n"
                f"📝 **Post**: {result.reddit_title or '(unknown)'}\n"
                f"🔗 **Reel**: {result.reel_url or 'N/A'}"
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
