"""
main.py

Entry point to run the Autoposter Discord Bot.
Runs 24/7 on your local machine while it's turned on.
"""

import sys

# Ensure UTF-8 output encoding on Windows consoles to prevent UnicodeEncodeError with emojis
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from bot.discord_bot import main

if __name__ == "__main__":
    main()
