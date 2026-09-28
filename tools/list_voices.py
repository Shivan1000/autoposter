"""
tools/list_voices.py

Utility to list and filter all available neural TTS voices (Edge-TTS and Kokoro).

Usage:
    python tools/list_voices.py
    python tools/list_voices.py --locale en-US
"""

import argparse
import asyncio
import sys

async def list_edge_voices(locale: str = "en-US"):
    try:
        import edge_tts
        voices = await edge_tts.list_voices()
        filtered = [v for v in voices if v["Locale"].startswith(locale)]
        print(f"=== Available Edge-TTS Voices ({locale}) ===")
        for v in filtered:
            print(f"  • {v['ShortName']:<30} [{v['Gender']}] ({v.get('FriendlyName', '')})")
    except ImportError:
        print("edge_tts is not installed.")

def list_kokoro_voices():
    print("\n=== Popular Kokoro TTS Local Voices ===")
    print("  • am_adam       (American Male - deep/calm)")
    print("  • am_michael    (American Male - energetic)")
    print("  • af_sarah      (American Female - clear/bright)")
    print("  • af_bella      (American Female - narrative)")

def main():
    parser = argparse.ArgumentParser(description="List available TTS voices")
    parser.add_argument("--locale", default="en-US", help="Locale filter (e.g. en-US, en-GB)")
    args = parser.parse_args()

    asyncio.run(list_edge_voices(args.locale))
    list_kokoro_voices()

if __name__ == "__main__":
    main()
