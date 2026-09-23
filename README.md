# 🎬 Autoposter — Reddit-to-Instagram Reels Bot

Autoposter is a modular Python 3.11+ automation system that:

1. Fetches trending posts from Reddit subreddits
2. Screenshots the thread, generates a voiceover script, and produces TTS audio
3. Composes a 1080×1920 vertical video with gameplay background, screenshot overlay, and synced subtitles
4. Publishes the video to Instagram as a Reel via the official Meta Graph API
5. Reports success/failure back to a Discord channel via slash commands

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Account Setup](#account-setup)
  - [Reddit API](#1-reddit-api)
  - [Discord Bot](#2-discord-bot)
  - [Anthropic Claude API](#3-anthropic-claude-api)
  - [Meta / Instagram Setup](#4-meta--instagram-setup)
  - [Cloudflare R2 Setup](#5-cloudflare-r2-setup-temp-video-hosting)
- [Configuration](#configuration)
- [Background Gameplay Clips](#background-gameplay-clips)
- [Running the Bot](#running-the-bot)
- [Manual Pipeline Test (No Discord)](#manual-pipeline-test-no-discord)
- [Project Structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Docker Deployment](#docker-deployment)

---

## Prerequisites

| Requirement | Minimum Version | Notes |
|---|---|---|
| Python | 3.11+ | Required for `asyncio` features used |
| FFmpeg | 6.0+ | Must be on PATH; includes ffprobe |
| Chromium | via Playwright | Installed automatically |
| yt-dlp | latest | Optional — only for gameplay downloader |

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/yourname/autoposter.git
cd autoposter

# 2. Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux / macOS

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright's Chromium browser
python -m playwright install chromium

# 5. Copy and fill in the environment file
copy .env.example .env
notepad .env
```

---

## Account Setup

### 1. Reddit API

1. Go to **https://www.reddit.com/prefs/apps** (must be logged in)
2. Click **"create another app…"**
3. Choose **"script"** type
4. Fill in any name and redirect URI (e.g. `http://localhost`)
5. Copy the **client ID** (shown under the app name) and **client secret**
6. Add to `.env`:
   ```ini
   REDDIT_CLIENT_ID=your_client_id
   REDDIT_CLIENT_SECRET=your_client_secret
   REDDIT_USER_AGENT=autoposter/1.0 by u/your_reddit_username
   ```

---

### 2. Discord Bot

1. Go to **https://discord.com/developers/applications**
2. Click **New Application** → give it a name
3. Go to **Bot** tab → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable:
   - **Server Members Intent**
   - **Message Content Intent**
5. Copy the **Bot Token** and add to `.env`:
   ```ini
   DISCORD_BOT_TOKEN=your_bot_token
   ```
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot` + `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`
7. Open the generated URL in your browser and invite the bot to your server
8. **Enable Developer Mode** in Discord (Settings → Advanced → Developer Mode)
9. Right-click your log channel → **Copy ID** → add to `.env`:
   ```ini
   DISCORD_LOG_CHANNEL_ID=your_channel_id
   ```

---

### 3. Anthropic Claude API

1. Sign up at **https://console.anthropic.com/**
2. Go to **API Keys** → create a new key
3. Add to `.env`:
   ```ini
   ANTHROPIC_API_KEY=sk-ant-...
   ```
> **Cost note**: The bot uses `claude-3-5-haiku` (cheapest Claude model). A typical run costs ~$0.001–0.003 in tokens.

---

### 4. Meta / Instagram Setup

> ⚠️ **This is the most involved setup step.** Instagram's Content Publishing API requires a Business/Creator account and a Facebook Developer App.

#### Step 4a — Create a Facebook Developer App

1. Go to **https://developers.facebook.com/** and log in
2. Click **My Apps → Create App**
3. Select **Business** type
4. Go to **Add Products** → add **Instagram Graph API**
5. Go to **App Settings → Basic** and note your **App ID** and **App Secret**:
   ```ini
   FACEBOOK_APP_ID=your_app_id
   FACEBOOK_APP_SECRET=your_app_secret
   ```

#### Step 4b — Link Instagram Business Account to a Facebook Page

1. Make sure your Instagram account is set to **Business** or **Creator**  
   (Instagram App → Settings → Account → Switch to Professional Account)
2. Link it to a **Facebook Page**:  
   Instagram App → Settings → Account → Linked Accounts → Facebook
3. In Facebook Business Suite: **Settings → Instagram Accounts** → connect your account

#### Step 4c — Get Your Instagram User ID

1. In the Facebook Developer console → **Tools → Graph API Explorer**
2. Select your app from the top dropdown
3. Under **Access Token**, choose your Facebook user token
4. Add the `instagram_basic`, `instagram_content_publish` permissions
5. Run: `GET /me/accounts` → find your Page
6. Run: `GET /{page_id}?fields=instagram_business_account` → get your IG user ID
7. Add to `.env`:
   ```ini
   INSTAGRAM_USER_ID=your_numeric_ig_user_id
   ```

#### Step 4d — Generate a Long-Lived Access Token

1. In **Graph API Explorer**, generate a User Access Token with these permissions:
   - `instagram_basic`
   - `instagram_content_publish`
   - `pages_show_list`
   - `pages_read_engagement`
2. Exchange for a **long-lived token** (60-day expiry):
   ```
   GET https://graph.facebook.com/v19.0/oauth/access_token?
       grant_type=fb_exchange_token&
       client_id={app_id}&
       client_secret={app_secret}&
       fb_exchange_token={short_lived_token}
   ```
3. Add the returned token to `.env`:
   ```ini
   INSTAGRAM_ACCESS_TOKEN=your_long_lived_token
   ```
4. **Token renewal**: Long-lived tokens auto-renew if used within 60 days. The bot logs a warning when the token is within 7 days of expiry. Renew manually at https://developers.facebook.com/tools/explorer/

> **App Review**: For posting to non-test accounts, `instagram_content_publish` requires **App Review**. During development, add test users in **App Roles → Testers** to bypass this requirement.

---

### 5. Cloudflare R2 Setup (Temp Video Hosting)

The Meta Graph API fetches your video from a public URL — it does not accept direct uploads. Cloudflare R2 has a generous free tier (10 GB storage, no egress fees).

1. Sign up at **https://dash.cloudflare.com/**
2. Go to **R2 → Create Bucket** → name it (e.g. `autoposter-videos`)
3. **Enable public access**: Bucket settings → Public Access → Allow Access
4. Note the **S3 API endpoint** from the bucket page
5. Go to **R2 → Manage R2 API Tokens** → Create a token with **Object Read & Write** permissions
6. Add to `.env`:
   ```ini
   R2_ACCOUNT_ID=your_cloudflare_account_id
   R2_ACCESS_KEY_ID=your_r2_api_key_id
   R2_SECRET_ACCESS_KEY=your_r2_api_key_secret
   R2_BUCKET_NAME=autoposter-videos
   R2_PUBLIC_BASE_URL=https://pub-xxxxxxxxxxxx.r2.dev
   ```
   > The public URL is shown in the bucket's **Public Access** settings.

---

## Configuration

All non-secret runtime settings are in [`config.yaml`](config.yaml). Key settings:

```yaml
subreddits:
  - AskReddit
  - tifu
  - AmItheAsshole

reddit:
  time_filter: day        # day | week | month
  candidate_pool: 10      # How many posts to evaluate
  allow_nsfw: false
  top_comments_count: 5
  min_score: 100

tts:
  voice: en-US-GuyNeural  # Run: edge-tts --list-voices
  max_words: 130

posting:
  cooldown_minutes: 60    # Min minutes between posts
```

---

## Background Gameplay Clips

The video composer needs **at least one `.mp4` file** in `assets/gameplay/`.

**Option A: Use the downloader tool**
```bash
# List available clips
python tools/download_gameplay.py --list

# Download from a royalty-free YouTube URL and split into 90s clips
python tools/download_gameplay.py --url "https://www.youtube.com/watch?v=..." --segment 90
```

**Option B: Manual placement**

Simply copy your own `.mp4` files into `assets/gameplay/`. The bot will randomly select and randomly time-offset each clip to avoid repetition.

> ⚠️ **Copyright**: Only use footage you have the right to use. Search YouTube for "Subway Surfers gameplay no copyright" or use your own gameplay recordings.

---

## Running the Bot

```bash
# Activate your virtual environment first
.venv\Scripts\activate

# Start the Discord bot
python -m bot.discord_bot

# Or via the installed entry point:
autoposter-bot
```

In Discord, use:
```
/generate subreddit:AskReddit tone:funny
/generate subreddit:tifu
/history limit:10
/status
```

---

## Manual Pipeline Test (No Discord)

Test the full pipeline on a subreddit without needing Discord:

```bash
# Dry run — runs everything except Instagram upload, saves video locally
python test_pipeline.py --subreddit AskReddit --dry-run

# Specific tone
python test_pipeline.py --subreddit tifu --tone funny --dry-run

# Real post (requires all credentials configured)
python test_pipeline.py --subreddit AskReddit

# Test individual stages:
python test_pipeline.py --stage screenshot --url "https://www.reddit.com/r/AskReddit/comments/..."
python test_pipeline.py --stage tts --text "This is a test script for TTS."
python test_pipeline.py --stage script --title "TIFU by emailing my boss instead of my wife"
python test_pipeline.py --stage video --screenshot tmp/s.png --audio tmp/a.mp3 --subtitles tmp/s.ass

# Debug mode
python test_pipeline.py --subreddit AskReddit --dry-run --log-level DEBUG
```

---

## Project Structure

```
autoposter/
├── .env.example              # Template for all required credentials
├── .gitignore
├── config.yaml               # Runtime configuration
├── requirements.txt
├── pyproject.toml
├── test_pipeline.py          # Manual pipeline test (no Discord needed)
│
├── pipeline/
│   ├── orchestrator.py       # Top-level pipeline runner
│   ├── reddit_fetcher.py     # PRAW-based Reddit post fetcher
│   ├── screenshotter.py      # Playwright screenshot of Reddit threads
│   ├── script_writer.py      # Claude API voiceover script generator
│   ├── tts_engine.py         # edge-tts audio + word-boundary timing
│   ├── subtitle_builder.py   # .ass subtitle file builder
│   ├── video_composer.py     # ffmpeg video compositor
│   ├── caption_generator.py  # Claude API Instagram caption generator
│   ├── instagram_publisher.py # Meta Graph API publisher (R2/S3 uploader)
│   └── dedup_store.py        # SQLite post deduplication store
│
├── bot/
│   └── discord_bot.py        # Discord slash command bot
│
├── utils/
│   ├── logging_config.py     # Rotating log file + console setup
│   ├── retry.py              # tenacity retry decorator factory
│   ├── temp_manager.py       # Per-job temp directory lifecycle
│   └── video_validator.py    # ffprobe video sanity checker
│
├── tools/
│   └── download_gameplay.py  # yt-dlp gameplay clip downloader
│
├── assets/
│   └── gameplay/             # Place .mp4 background clips here
│
└── data/
    └── dedup.db              # SQLite DB (auto-created, gitignored)
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `REDDIT_CLIENT_ID must be set` | Missing .env | Copy .env.example → .env and fill in |
| `No .mp4 files found in assets/gameplay` | No background clips | Run `tools/download_gameplay.py` or add clips manually |
| `ffmpeg not found` | FFmpeg not installed/on PATH | Install FFmpeg and add to system PATH |
| `playwright._impl._errors.Error` | Chromium not installed | Run `python -m playwright install chromium` |
| `MetaAPIError 400: invalid video` | Video format mismatch | Check ffprobe output; ensure H.264/AAC/MP4/faststart |
| `MetaAPIError 190: expired token` | Access token expired | Refresh at https://developers.facebook.com/tools/explorer/ |
| `Container entered ERROR state` | Meta rejected video | Check duration (3–90s), resolution (min 540x960), codec |
| `No eligible posts found` | All posts filtered | Lower `min_score` in config.yaml or try `time_filter: week` |
| Discord commands not showing | Slash commands not synced | Restart the bot; commands sync on startup |

---

## Docker Deployment

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install chromium --with-deps

COPY . .

CMD ["python", "-m", "bot.discord_bot"]
```

```bash
# Build and run
docker build -t autoposter .
docker run -d \
  --env-file .env \
  --volume $(pwd)/assets:/app/assets \
  --volume $(pwd)/data:/app/data \
  --volume $(pwd)/logs:/app/logs \
  --name autoposter \
  autoposter
```

---

## License

MIT — see [LICENSE](LICENSE) for details.

> **Disclaimer**: This tool uses the official Reddit API and Meta Graph API per their respective Terms of Service. You are responsible for ensuring your content complies with Instagram's Community Guidelines and Reddit's API Terms.
