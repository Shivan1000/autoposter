# 🎬 Autoposter — Reddit-to-Instagram Reels Bot

Autoposter is an end-to-end automated Python system that turns trending Reddit memes and discussions into viral, vertical 9:16 Instagram Reels with Minecraft parkour gameplay, dynamic synced subtitles, realistic voiceover, and custom screenshot cards featuring real images and top comments.

You can trigger and control everything directly from a **Discord Bot** with live previews and 1-click publishing.

---

## ⚡ Highlights & Key Features

* **🎙️ Kokoro TTS (Default Voice: `am_adam`)**: Built-in integration with [Kokoro-82M](https://github.com/hexgrad/kokoro) delivering ultra-realistic, natural voiceovers locally without needing API keys or subscriptions!
* **🌐 Multiple TTS Engines Supported**:
  * **Kokoro TTS** (Default, local & free)
  * **Fish.Audio** (Custom voice cloning via API key & voice reference ID)
  * **ElevenLabs** (Premium voice cloning)
  * **Edge-TTS** (Automatic high-speed zero-config fallback)
* **🖼️ Meme Cards with Single Top Comment**: Automatically scrapes Reddit memes with images and renders a sleek, dark-mode card showcasing the post title, meme image, upvotes, and the **#1 top comment** from the thread.
* **🤣 Comedic & Exaggerated Scripts**: AI script writer powered by Gemini crafts wild, energetic, unhinged comedic commentary tailored specifically to the meme image and title.
* **📱 True 9:16 Mobile Aspect Ratio**: Automated Playwright browser upload automatically interacts with Instagram's crop selector to force `9:16 / Original` vertical reel format, avoiding 1:1 square cropping.
* **🚫 Zero Reddit API Key Required**: Fetches trending posts, top comments, and images directly using public JSON/RSS feeds.
* **🤖 Discord Bot Controls**:
  * `/thread` or `/random` — Generates a random Reddit post preview card, voiceover script, and viral Instagram caption with interactive buttons.
  * `/post` — Creates and publishes a Reel directly to Instagram.
  * `/dryrun` — Renders the video locally for previewing without uploading.
  * Direct clickable profile link (`https://www.instagram.com/your_username/`) on completion.
* **⏱️ 40+ Second Long Videos**: Engagement-optimized script lengths (110–140 words).
* **🔤 Dynamic Synced Subtitles**: High-contrast, bold subtitles (`78px Arial Black`, tuned margin) positioned comfortably above Instagram's bottom navigation and caption UI.
* **🧱 Vertical 9:16 Minecraft Parkour Background**: Dynamic background gameplay with randomized start offsets.
* **💾 Deduplication Store**: SQLite database prevents duplicate posts from ever being processed twice.

---

## 📋 Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Start Installation](#quick-start-installation)
- [Environment Configuration](#environment-configuration)
- [Configuration (`config.yaml`)](#configuration-configyaml)
- [Discord Bot Setup](#discord-bot-setup)
- [Instagram Setup](#instagram-setup)
- [Discord Bot Commands](#discord-bot-commands)
- [Running the System](#running-the-system)
- [Project Architecture](#project-architecture)
- [Troubleshooting](#troubleshooting)

---

## 🛠️ Prerequisites

| Requirement | Details |
|---|---|
| **Python** | 3.10 or 3.11+ |
| **FFmpeg** | Must be installed and available on system PATH (`ffmpeg -version`) |
| **Chromium** | Installed automatically via Playwright (`python -m playwright install chromium`) |
| **Assets** | Minecraft parkour gameplay MP4 clips in `assets/gameplay/` |

---

## 🚀 Quick Start Installation

```bash
# 1. Clone repository
git clone https://github.com/Shivan1000/autoposter.git
cd autoposter

# 2. Set up virtual environment
python -m venv .venv
.venv\Scripts\activate        # On Windows
# source .venv/bin/activate   # On Linux/macOS

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright browser
python -m playwright install chromium

# 5. Configure environment variables
copy .env.example .env
notepad .env
```

---

## ⚙️ Environment Configuration (`.env`)

Configure your `.env` file with the following variables:

```ini
# --- Discord Bot ---
DISCORD_BOT_TOKEN=your_discord_bot_token_here

# --- AI Script & Caption Generation ---
GEMINI_API_KEY=your_gemini_api_key_here

# --- Instagram (for automated browser login) ---
INSTAGRAM_USERNAME=your_instagram_username
INSTAGRAM_PASSWORD=your_instagram_password

# --- Text-to-Speech (TTS) ---
# Kokoro TTS works 100% locally and free with no API keys!
# Optional: Fish Audio API key & voice reference ID:
FISH_AUDIO_API_KEY=sk-fish-xxxx
FISH_AUDIO_VOICE_ID=c494e0005f3544f3b1bf98c6ffb4c645

# Optional: ElevenLabs
ELEVENLABS_API_KEY=your_elevenlabs_api_key_optional
```

> **Note on Reddit**: No Reddit API keys (`REDDIT_CLIENT_ID` or `REDDIT_CLIENT_SECRET`) are required! The scraper extracts posts and renders screenshot cards automatically.

---

## 🛠️ Configuration (`config.yaml`)

You can customize video styles, subreddits, and voice settings in `config.yaml`:

```yaml
# Select TTS Provider: "kokoro" | "fish-audio" | "edge-tts" | "elevenlabs"
tts:
  provider: kokoro
  kokoro:
    voice: "am_adam"     # Adam voice from Kokoro-82M
  voice: en-US-ChristopherNeural  # Fallback voice

# Reddit Post Filtering
reddit:
  require_images: true   # Focus exclusively on meme images
  sort: random           # hot | top | new | rising | random
  subreddits:
    - memes
    - dankmemes
    - me_irl
    - wholesomememes
    - funny
```

---

## 🤖 Discord Bot Setup

1. Visit the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** → give it a name (e.g. `Autoposter`).
3. Go to the **Bot** tab → click **Reset Token** to copy your **Bot Token**. Paste it into `.env` as `DISCORD_BOT_TOKEN`.
4. Go to **OAuth2 → URL Generator**:
   - Check `bot` and `applications.commands`.
   - Bot Permissions: `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`.
5. Open the generated invite link in your browser and add the bot to your server.

---

## 📸 Instagram Setup

The bot uses Playwright to log into Instagram and publish Reels directly:
1. Enter your `INSTAGRAM_USERNAME` and `INSTAGRAM_PASSWORD` in `.env`.
2. On first run, it logs in, handles popups, and saves session cookies to `data/instagram_session.json`.
3. Subsequent runs use the saved session directly without needing to log in again.
4. The uploader automatically sets the **9:16 vertical crop** so videos display edge-to-edge without letterboxing or square distortion.

---

## 🎮 Discord Bot Commands

You can use either **Slash Commands** (`/`) or **Prefix Commands** (`!` or `?`):

| Command | Description | Example |
|---|---|---|
| `/thread [subreddit] [tone]` | Fetches a random Reddit thread with screenshot card, AI narration preview, caption, and **1-click Post button** | `/thread subreddit:memes` |
| `/random` | Quick shortcut to roll a random meme thread | `/random` |
| `/post [subreddit] [tone]` | Directly generates and publishes a 40+ second Reel to Instagram | `/post subreddit:memes tone:funny` |
| `/dryrun [subreddit]` | Renders the video locally without uploading (saved to `tmp/`) | `/dryrun subreddit:dankmemes` |
| `/history [limit]` | Shows recently posted Reels from the deduplication database | `/history limit:5` |
| `/status` | Displays bot uptime, total reels posted, and latency | `/status` |

---

## 🏃 Running the System

### Option A: Run the Discord Bot
```bash
python main.py
```
*(Or double-click `run_bot.bat` on Windows)*

### Option B: Run a Manual Pipeline Test from CLI
```bash
# Dry run test (renders video without publishing)
python test_pipeline.py --subreddit memes --dry-run

# Real post directly from command line
python test_pipeline.py --subreddit memes
```

---

## 🏗️ Project Architecture

```
autoposter/
├── assets/
│   └── gameplay/              # 1080x1920 vertical background MP4 gameplay clips
├── bot/
│   └── discord_bot.py         # Discord slash commands, interactive views & embeds
├── config.yaml                # Video dimensions, fonts, subtitle margins & TTS options
├── data/
│   ├── dedup.db               # SQLite database tracking processed Reddit posts
│   └── instagram_session.json # Saved Instagram browser session cookies
├── pipeline/
│   ├── caption_generator.py   # Viral hook, teaser & hashtag generator
│   ├── dedup_store.py         # Thread deduplication engine
│   ├── instagram_browser.py   # Automated Playwright browser publisher (9:16 crop handler)
│   ├── orchestrator.py        # 7-stage pipeline coordinator
│   ├── reddit_scraper.py      # Zero-API Reddit scraper & card screenshotter with top comment
│   ├── script_writer.py       # Comedic exaggerated AI voiceover script generator
│   ├── subtitle_builder.py    # Subtitle generator (.ass) with custom styling
│   ├── tts_engine.py          # Multi-provider TTS engine (Kokoro, Fish Audio, ElevenLabs, Edge-TTS)
│   └── video_composer.py      # FFmpeg video composer (overlay + audio + subtitles)
├── main.py                    # Application entrypoint
├── requirements.txt           # Python dependencies
└── test_pipeline.py           # CLI test runner
```

---

## 🔧 Troubleshooting

| Issue | Cause | Solution |
|---|---|---|
| `ffmpeg not found` | FFmpeg missing from system PATH | Install FFmpeg and add the `bin` directory to your environment variables |
| `No .mp4 files found in assets/gameplay` | Missing background video | Place at least one 1080×1920 MP4 file in `assets/gameplay/` |
| `Playwright browser not found` | Chromium binary not installed | Run `python -m playwright install chromium` |
| `Kokoro weights download error` | First run model caching | Weights download automatically on the first run; ensure an active internet connection |
| `Rate limit / Quota exceeded` | Gemini API key limit | The pipeline includes automatic model fallbacks; ensure your `GEMINI_API_KEY` is active |

---

## 📄 License
MIT License. Feel free to modify and use for your own automated video creation workflows.
