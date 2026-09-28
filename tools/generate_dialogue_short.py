"""
tools/generate_dialogue_short.py

CLI tool to generate Dr. Ethan Kwan style dual-voiceover comedy shorts.

Usage:
    python tools/generate_dialogue_short.py --preset instagram_bot --output output_bot.mp4
    python tools/generate_dialogue_short.py --preset gaming_pc --output output_pc.mp4
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.dialogue_short_generator import DialogueShortGenerator, PRESET_SCRIPTS

async def main():
    parser = argparse.ArgumentParser(description="Generate Dual-Voiceover Comedy Short (Ethan Kwan Style)")
    parser.add_argument("--preset", choices=list(PRESET_SCRIPTS.keys()), default="instagram_bot", help="Preset script to use")
    parser.add_argument("--output", default="dialogue_short.mp4", help="Output video path")
    parser.add_argument("--rate", default="+16%", help="Speech rate adjustment (default: +16 percent)")
    args = parser.parse_args()

    dialogue = PRESET_SCRIPTS[args.preset]
    generator = DialogueShortGenerator()
    
    print(f"Generating short for preset '{args.preset}' to '{args.output}'...")
    res = await generator.generate_short(dialogue=dialogue, output_path=args.output, rate=args.rate)
    print(f"Done! Video generated at: {res}")

if __name__ == "__main__":
    asyncio.run(main())
