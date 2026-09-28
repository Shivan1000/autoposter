"""
bot/discord_bot.py

Discord bot entrypoint with slash commands and prefix commands.

Commands:
  /post [subreddit] [tone] or !post [subreddit]
    → Triggers the full pipeline for the given subreddit
    → Sends live progress updates + final result embed to the channel

  /thread [subreddit] [tone] or !thread [subreddit]
    → Fetches Reddit post card, generates voiceover & caption preview
    → Interactive buttons: [Post Reel to Instagram] [Another Thread]

  /dryrun [subreddit] [tone] or !dryrun [subreddit]
    → Generates video without uploading to Instagram (test render)

  /history [limit] or !history
    → Shows recently posted Reels

  /status or !status
    → Shows bot uptime, health, and latency

  /help or !help
    → Lists all available commands
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from pipeline.caption_generator import CaptionGenerator
from pipeline.dedup_store import DedupStore
from pipeline.orchestrator import PipelineResult, run_pipeline
from pipeline.reddit_scraper import RedditScraper
from pipeline.script_writer import ScriptWriter
from utils.logging_config import setup_logging

load_dotenv()
logger = logging.getLogger(__name__)

_VALID_TONES = ["funny", "uncensored", "unhinged", "dramatic", "curious", "shocked"]
_EMBED_COLOR_SUCCESS = discord.Color.from_rgb(0, 200, 100)
_EMBED_COLOR_FAILURE = discord.Color.from_rgb(220, 50, 50)
_EMBED_COLOR_INFO = discord.Color.from_rgb(88, 101, 242)  # Discord blurple

_START_TIME = datetime.now(timezone.utc)


class ThreadPreviewView(discord.ui.View):
    """Interactive view for generated Reddit thread previews."""

    def __init__(self, subreddit: str, tone: str, post_title: str) -> None:
        super().__init__(timeout=600)
        self.subreddit = subreddit
        self.tone = tone
        self.post_title = post_title

    @discord.ui.button(label="🎬 Post Reel to Instagram", style=discord.ButtonStyle.success, emoji="🚀")
    async def post_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                if interaction.message:
                    await interaction.message.edit(view=self)
        except Exception as exc:
            logger.debug("Could not disable buttons: %s", exc)

        progress_msg = await interaction.followup.send(
            embed=_build_progress_embed(self.subreddit, self.tone, stage="🚀 Creating Reel & publishing to Instagram…"),
            ephemeral=False,
        )
        asyncio.create_task(
            _run_pipeline_and_report(interaction, self.subreddit, self.tone, dry_run=False, status_message=progress_msg)
        )

    @discord.ui.button(label="🎲 Another Thread", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def another_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                if interaction.message:
                    await interaction.message.edit(view=self)
        except Exception as exc:
            logger.debug("Could not edit view: %s", exc)

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

    loading_embed = discord.Embed(
        title=f"🔍 Fetching Random Thread from r/{chosen_sub}...",
        description="⏳ Scraping post, capturing image card, and writing AI voiceover preview…",
        color=_EMBED_COLOR_INFO,
    )

    is_interaction = hasattr(interaction_or_ctx, "response")
    msg = None

    if is_interaction:
        interaction = interaction_or_ctx
        if is_followup:
            msg = await interaction.followup.send(embed=loading_embed)
        elif not interaction.response.is_done():
            await interaction.response.send_message(embed=loading_embed)
        else:
            msg = await interaction.followup.send(embed=loading_embed)
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

        file = discord.File(str(screenshot_path.resolve()), filename="card.png")
        embed.set_image(url="attachment://card.png")

        embed.add_field(
            name=f"🎙️ AI Narration Script ({words} words • ~{est_sec}s Reel)",
            value=f"```\n{script[:600]}\n```",
            inline=False,
        )

        embed.add_field(
            name="📱 Viral Instagram Caption & Tags",
            value=f"{caption_obj.text[:300]}\n\n*{' '.join(caption_obj.hashtags[:6])}*",
            inline=False,
        )

        embed.set_footer(text="Click 'Post Reel to Instagram' below to automatically create and publish this Reel!")

        view = ThreadPreviewView(chosen_sub, chosen_tone, post.title)

        if is_interaction:
            interaction = interaction_or_ctx
            if not is_followup:
                try:
                    await interaction.edit_original_response(embed=embed, attachments=[file], view=view)
                    return
                except Exception as exc:
                    logger.debug("Could not edit original response: %s", exc)

            await interaction.followup.send(embed=embed, file=file, view=view)
            if msg and hasattr(msg, "delete"):
                try:
                    await msg.delete()
                except Exception:
                    pass
        else:
            if msg:
                try:
                    await msg.delete()
                except Exception:
                    pass
            await interaction_or_ctx.send(embed=embed, file=file, view=view)

    except Exception as exc:
        logger.exception("Error generating random thread: %s", exc)
        err_embed = discord.Embed(
            title="❌ Could Not Fetch Thread",
            description=f"Failed to fetch from r/{chosen_sub}:\n```{exc}```",
            color=_EMBED_COLOR_FAILURE,
        )
        if is_interaction:
            interaction = interaction_or_ctx
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=err_embed)
                else:
                    await interaction.response.send_message(embed=err_embed)
            except Exception:
                if interaction.channel:
                    await interaction.channel.send(embed=err_embed)
        else:
            await interaction_or_ctx.send(embed=err_embed)


class AutoposterBot(commands.Bot):
    """Custom commands.Bot instance for Autoposter."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned_or("!", "?"),
            intents=intents,
            help_command=None,
        )
        self.dedup_store = DedupStore()

    async def setup_hook(self) -> None:
        await self.dedup_store.init()


bot = AutoposterBot()


# ---------------------------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="thread",
    description="🎲 Generate a random Reddit thread with screenshot card, script & caption preview",
)
@app_commands.describe(
    subreddit="Subreddit name (default: random funny subreddit, or e.g. memes, me_irl)",
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
    subreddit="Subreddit name (e.g. memes, r/memes, dankmemes)",
    tone="Script/caption tone style (select 'uncensored' for 18+ raw comedy, or funny, unhinged)",
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

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=False)

    msg = await interaction.followup.send(
        embed=_build_progress_embed(subreddit, tone_value, stage="🚀 Starting Reel generation pipeline…"),
    )

    asyncio.create_task(
        _run_pipeline_and_report(interaction, subreddit, tone_value, dry_run=False, status_message=msg)
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

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=False)

    msg = await interaction.followup.send(
        embed=_build_progress_embed(subreddit, tone_value, stage="🧪 Rendering test Reel (dry-run)…"),
    )

    asyncio.create_task(
        _run_pipeline_and_report(interaction, subreddit, tone_value, dry_run=True, status_message=msg)
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
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ Invalid subreddit name. Use only letters, numbers, and underscores.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ Invalid subreddit name. Use only letters, numbers, and underscores.",
                ephemeral=True,
            )
        return

    tone_value = tone.value if tone else None

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=False)

    msg = await interaction.followup.send(
        embed=_build_progress_embed(subreddit, tone_value, stage="🚀 Starting Reel generation pipeline…"),
    )

    asyncio.create_task(
        _run_pipeline_and_report(interaction, subreddit, tone_value, dry_run=False, status_message=msg)
    )


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
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "📭 No Reels have been posted yet.", ephemeral=True
            )
        else:
            await interaction.followup.send(
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

    if not interaction.response.is_done():
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, ephemeral=True)


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
    embed.add_field(name="Bot latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Connected Servers", value=str(len(bot.guilds)), inline=True)

    if not interaction.response.is_done():
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Text Prefix Commands (!thread, ?thread, !post, ?post, !dryrun, !status, !help)
# Works with prefix AND with @mention: @autopostbot post
# ---------------------------------------------------------------------------

@bot.command(name="thread", aliases=["random", "getthread"])
async def prefix_thread(ctx: commands.Context, subreddit: str = "random") -> None:
    """Text command: !thread or ?thread [subreddit]"""
    await _generate_and_send_thread(ctx, subreddit=subreddit, tone="funny")


@bot.command(name="post")
async def prefix_post(ctx: commands.Context, subreddit: str = "memes", tone: str = "funny") -> None:
    """Text command: !post [subreddit] [tone] (e.g. !post r/memes uncensored)"""
    sub = subreddit.strip().lstrip("r/").strip("/").lower()
    chosen_tone = tone.strip().lower()
    msg = await ctx.send(embed=_build_progress_embed(sub, chosen_tone, stage="🚀 Starting Reel generation pipeline…"))
    try:
        result = await run_pipeline(sub, tone=chosen_tone, dry_run=False)
        embed = _build_result_embed(result)
        try:
            await msg.edit(embed=embed)
        except Exception:
            await ctx.send(embed=embed)
    except Exception as exc:
        logger.exception("Error in !post command: %s", exc)
        err_embed = discord.Embed(
            title="💥 Pipeline Failed",
            description=f"```{exc}```",
            color=_EMBED_COLOR_FAILURE,
        )
        try:
            await msg.edit(embed=err_embed)
        except Exception:
            await ctx.send(embed=err_embed)


@bot.command(name="dryrun")
async def prefix_dryrun(ctx: commands.Context, subreddit: str = "memes", tone: str = "funny") -> None:
    """Text command: !dryrun [subreddit] [tone] (e.g. !dryrun r/memes uncensored)"""
    sub = subreddit.strip().lstrip("r/").strip("/").lower()
    chosen_tone = tone.strip().lower()
    msg = await ctx.send(embed=_build_progress_embed(sub, chosen_tone, stage="🧪 Rendering test Reel (dry-run)…"))
    try:
        result = await run_pipeline(sub, tone=chosen_tone, dry_run=True)
        embed = _build_result_embed(result)
        try:
            await msg.edit(embed=embed)
        except Exception:
            await ctx.send(embed=embed)
    except Exception as exc:
        logger.exception("Error in !dryrun command: %s", exc)
        await ctx.send(f"❌ Error: {exc}")


@bot.command(name="status")
async def prefix_status(ctx: commands.Context) -> None:
    """Text command: !status"""
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
    embed.add_field(name="Bot latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Connected Servers", value=str(len(bot.guilds)), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="history")
async def prefix_history(ctx: commands.Context, limit: int = 5) -> None:
    """Text command: !history [limit]"""
    limit = max(1, min(limit, 20))
    posts = await bot.dedup_store.get_recent(limit)
    if not posts:
        await ctx.send("📭 No Reels have been posted yet.")
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
    await ctx.send(embed=embed)


@bot.command(name="help")
async def prefix_help(ctx: commands.Context) -> None:
    """Text command: !help"""
    embed = discord.Embed(
        title="📖 Autoposter Commands",
        description="You can use slash commands (`/`), text commands (`!`), or mention the bot (@autopostbot):",
        color=_EMBED_COLOR_INFO,
    )
    embed.add_field(
        name="🎬 Create & Post Reels",
        value=(
            "• `/post [subreddit] [tone]` or `!post [subreddit]`\n"
            "  *Full pipeline: scrape Reddit meme, AI script & voiceover, compose 9:16 video, and publish to Instagram Reels.*\n\n"
            "• `/thread [subreddit]` or `!thread`\n"
            "  *Preview Reddit post card, AI voiceover script & caption with instant 'Post' button.*\n\n"
            "• `/dryrun [subreddit]` or `!dryrun`\n"
            "  *Full test run rendering the video without publishing to Instagram.*"
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 Management & Info",
        value=(
            "• `/status` or `!status` — View uptime and connection latency\n"
            "• `/history` or `!history` — View recently published Reels\n"
            "• `/help` or `!help` — Show this guide"
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# Background Pipeline Reporter
# ---------------------------------------------------------------------------

async def _run_pipeline_and_report(
    interaction: discord.Interaction,
    subreddit: str,
    tone: str | None,
    dry_run: bool = False,
    status_message: discord.Message | discord.WebhookMessage | None = None,
) -> None:
    """Run the pipeline and send the result back via status message edit or followup."""
    try:
        result = await run_pipeline(subreddit, tone=tone, dry_run=dry_run)
        embed = _build_result_embed(result)

        delivered = False
        if status_message:
            try:
                await status_message.edit(embed=embed)
                delivered = True
            except Exception as exc:
                logger.debug("Could not edit initial status message: %s", exc)

        if not delivered:
            try:
                await interaction.followup.send(embed=embed)
                delivered = True
            except Exception as exc:
                logger.debug("Could not send followup message: %s", exc)

        if not delivered and interaction.channel:
            try:
                await interaction.channel.send(embed=embed)
            except Exception as exc:
                logger.warning("Could not send embed to channel: %s", exc)

    except Exception as exc:
        logger.exception("Unhandled error in pipeline task: %s", exc)
        error_embed = discord.Embed(
            title="💥 Pipeline Failed",
            description=f"Something went wrong:\n```{exc}```",
            color=_EMBED_COLOR_FAILURE,
        )
        delivered = False
        if status_message:
            try:
                await status_message.edit(embed=error_embed)
                delivered = True
            except Exception:
                pass
        if not delivered:
            try:
                await interaction.followup.send(embed=error_embed)
                delivered = True
            except Exception:
                pass
        if not delivered and interaction.channel:
            try:
                await interaction.channel.send(embed=error_embed)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (id=%s)", bot.user, bot.user.id)

    # 1. Global slash command sync
    try:
        synced_global = await bot.tree.sync()
        logger.info("Synced %d global slash commands", len(synced_global))
    except Exception as exc:
        logger.warning("Could not sync global slash commands: %s", exc)

    # 2. Clear any lingering guild-specific commands so Discord never displays duplicates
    for guild in bot.guilds:
        try:
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
            logger.debug("Cleaned guild commands for '%s'", guild.name)
        except Exception as exc:
            logger.debug("Could not clear guild commands for '%s': %s", guild.name, exc)

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="Reddit for content 👀 | /post or !post",
        )
    )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    await bot.process_commands(message)


@bot.event
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    logger.error("Slash command error: %s", error)
    msg = f"❌ Error running command: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        if interaction.channel:
            try:
                await interaction.channel.send(msg)
            except Exception:
                pass


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    logger.error("Prefix command error: %s", error)
    try:
        await ctx.send(f"❌ Error: {error}")
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
