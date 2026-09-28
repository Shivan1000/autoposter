"""
pipeline/dialogue_short_generator.py

Generates viral dual-voiceover comedy shorts (Dr. Ethan Kwan style) featuring:
  1. Two distinct, contrasting AI voiceovers (e.g. High-energy/delusional vs Deadpan/sarcastic)
  2. Rapid conversational ping-pong with tight inter-turn pauses (0.15s - 0.20s)
  3. Clean vertical 1080x1920 gameplay backdrop (Minecraft parkour)
  4. Subtle comedic background music ducked under dialogue
  5. Subtitle-free clean visuals (or optional captions)

Usage:
    from pipeline.dialogue_short_generator import DialogueShortGenerator
    generator = DialogueShortGenerator()
    await generator.generate_short(
        dialogue=[("delusional", "Hey..."), ("straight", "Yeah?")],
        output_path="output.mp4"
    )
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

PRESET_SCRIPTS = {
    "instagram_bot": [
        ("delusional", "Hey, did you check Instagram today?"),
        ("straight", "No. I was sleeping. Why?"),
        ("delusional", "I automated our entire social media pipeline using Python."),
        ("straight", "We do not have a social media pipeline. We are an accounting firm."),
        ("delusional", "Exactly. Untapped market."),
        ("straight", "What did you code?"),
        ("delusional", "A headless browser script. It logs into Instagram and auto-comments on trending posts."),
        ("straight", "What does it comment?"),
        ("delusional", "I hooked it up to a free AI model with zero filtering."),
        ("straight", "Zero filtering."),
        ("delusional", "Cuts down on latency. Speed is everything."),
        ("straight", "How many accounts did it comment on?"),
        ("delusional", "Forty-two thousand."),
        ("straight", "In one night."),
        ("delusional", "The script runs on twenty threads."),
        ("straight", "Where did it post?"),
        ("delusional", "Well, the CEO of our biggest client posted that his dog passed away."),
        ("straight", "Oh no."),
        ("delusional", "My bot commented: Huge L bro, grind never stops, link in bio for ten percent off crypto."),
        ("straight", "Did you delete it?"),
        ("delusional", "Could not. The bot auto-blocked him for negative energy."),
        ("straight", "It blocked our client."),
        ("delusional", "He was bringing down the algorithm."),
        ("straight", "Is the script still running?"),
        ("delusional", "Yeah, it is currently arguing with the official IRS account about tax evasion."),
        ("straight", "With the IRS."),
        ("delusional", "Bot is winning though. It called them mid."),
        ("straight", "I am packing my bags."),
        ("delusional", "Wait, our engagement is up six hundred percent."),
        ("straight", "Those are subpoena notices."),
        ("delusional", "Growth is growth.")
    ],
"retaining_wall": [
        ("delusional", "This wall is four inches on my property."),
        ("straight", "Four inches? Yes. Devastating."),
        ("delusional", "I had it surveyed. Move it by Friday."),
        ("straight", "It is a retaining wall. It holds your yard up."),
        ("delusional", "Not my problem. Move it or I bulldoze."),
        ("straight", "Okay. I will remove it."),
        ("delusional", "Finally. Wasn't that hard? Enjoy your weekend."),
        ("straight", "It is Friday. Your four inches are free now."),
        ("delusional", "Wait, my yard is sinking. Half my patio dropped!"),
        ("straight", "Dang."),
        ("delusional", "My hot tub is sideways!"),
        ("straight", "Technically diagonal."),
        ("delusional", "Put the wall back!"),
        ("straight", "Cannot. Do not want to trespass on your four inches."),
        ("delusional", "Contractor quoted me thirty-one thousand to fix it!"),
        ("straight", "Should have kept the free one."),
        ("delusional", "My hot tub is in the mud!"),
        ("straight", "Legally, it is your mud.")
    ],
    "tornado_grandpa": [
        ("delusional", "Grandpa please answer! News said you were missing!"),
        ("straight", "Missing? I am at Waffle House."),
        ("delusional", "What? How are you alive? Your house is gone!"),
        ("straight", "Tornado was not that bad. Saved on gas."),
        ("delusional", "It threw you into Georgia! You live in Alabama!"),
        ("straight", "Watch said eighteen thousand steps. Free workout."),
        ("delusional", "Did you break anything?"),
        ("straight", "Three ribs, shoulder, maybe hip. Doctor said no gym for six weeks."),
        ("delusional", "Good. Rest."),
        ("straight", "Disrespectful. It was chest day."),
        ("delusional", "You are 127 years old!"),
        ("straight", "Built different. Met lady here with bingo money and a Cadillac."),
        ("delusional", "Grandpa please go home!"),
        ("straight", "House gone. Tornado did remodeling. Living at her place."),
        ("delusional", "I am calling mom!"),
        ("straight", "Do not. If she thinks I am dead, she cannot ask me to fix her fence.")
    ],
    "gaming_pc": [
        ("delusional", "Hey, you know computers, right?"),
        ("straight", "Yeah. Why?"),
        ("delusional", "I fixed your gaming PC while you were at work."),
        ("straight", "Nothing was wrong with it."),
        ("delusional", "Bro, it was dangerously loud."),
        ("straight", "Those are the cooling fans."),
        ("delusional", "Exactly. So I unplugged them. Dead silent now."),
        ("straight", "You unplugged all the cooling fans?"),
        ("delusional", "All six of them. Total peace and quiet."),
        ("straight", "What about the processor?"),
        ("delusional", "Way ahead of you. It had this gross gray sticky gunk on it."),
        ("straight", "That is thermal paste."),
        ("delusional", "Well, it looked expired. So I scraped it off with a butter knife and washed it in the sink."),
        ("straight", "You washed the CPU in the sink."),
        ("delusional", "With Dawn dish soap. Cuts through grease instantly."),
        ("straight", "Dawn dish soap."),
        ("delusional", "Then I replaced the paste with something 100% natural."),
        ("straight", "What did you put on my $800 processor?"),
        ("delusional", "Organic peanut butter."),
        ("straight", "Peanut butter."),
        ("delusional", "Creamy, not crunchy. I am not an animal."),
        ("straight", "Is the PC plugged in right now?"),
        ("delusional", "Yeah, I am stress-testing it on Cyberpunk at max settings."),
        ("straight", "How does it smell?"),
        ("delusional", "Like a bakery. Fresh toasty peanut cookies."),
        ("straight", "Are there flames?"),
        ("delusional", "Just warm ambient lighting."),
        ("straight", "Turn it off."),
        ("delusional", "Can not. Power button melted into the case."),
        ("straight", "Unplug it from the wall."),
        ("delusional", "Bro, wait. Look at the graphics."),
        ("straight", "What about the graphics?"),
        ("delusional", "Crisp. Absolutely crisp."),
        ("straight", "The screen is black."),
        ("delusional", "Cinematic black bars. Very immersive."),
        ("straight", "I am calling the fire department."),
        ("delusional", "Tell them to bring chips. The dip is warming up.")
    ]
}


class DialogueShortGenerator:
    def __init__(
        self,
        voice_a: str = "en-US-ChristopherNeural",
        voice_b: str = "en-US-GuyNeural",
        gameplay_dir: Optional[str] = None,
        tmp_dir: Optional[str] = None,
    ):
        self.voice_a = voice_a  # Delusional / expressive
        self.voice_b = voice_b  # Deadpan / straight-man
        self.base_dir = Path(__file__).resolve().parent.parent
        self.gameplay_dir = Path(gameplay_dir) if gameplay_dir else self.base_dir / "assets" / "gameplay"
        self.tmp_dir = Path(tmp_dir) if tmp_dir else self.base_dir / "tmp" / "dialogue_shorts"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    async def generate_short(
        self,
        dialogue: List[Tuple[str, str]],
        output_path: str,
        bg_music_path: Optional[str] = None,
        rate: str = "+16%",
        pause_sec: float = 0.18,
    ) -> str:
        """Generate full dual-voiceover short video without subtitles."""
        import edge_tts

        output_file = Path(output_path).resolve()
        output_file.parent.mkdir(parents=True, exist_ok=True)
        session_id = f"run_{random.randint(1000, 9999)}"
        work_dir = self.tmp_dir / session_id
        work_dir.mkdir(parents=True, exist_ok=True)

        silence_path = work_dir / "silence.wav"
        silence_end_path = work_dir / "silence_end.wav"

        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", str(pause_sec), str(silence_path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "0.6", str(silence_end_path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        concat_list = []
        logger.info(f"Synthesizing {len(dialogue)} dialogue turns via Edge-TTS...")

        for idx, (speaker, line) in enumerate(dialogue):
            voice = self.voice_a if speaker.lower() in ["delusional", "a", "instigator"] else self.voice_b
            mp3_p = work_dir / f"turn_{idx:02d}_{speaker}.mp3"
            wav_p = work_dir / f"turn_{idx:02d}_{speaker}.wav"

            comm = edge_tts.Communicate(line, voice, rate=rate)
            await comm.save(str(mp3_p))

            subprocess.run(
                ["ffmpeg", "-y", "-i", str(mp3_p), "-ar", "48000", "-ac", "2", str(wav_p)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

            p_wav = str(wav_p.resolve()).replace("\\", "/")
            p_sil = str(silence_path.resolve()).replace("\\", "/")
            concat_list.append(f"file '{p_wav}'")
            concat_list.append(f"file '{p_sil}'")

        p_end = str(silence_end_path.resolve()).replace("\\", "/")
        concat_list.append(f"file '{p_end}'")

        concat_txt = work_dir / "concat_list.txt"
        concat_txt.write_text("\n".join(concat_list), encoding="utf-8")

        combined_voice = work_dir / "voiceover.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt), "-c", "copy", str(combined_voice)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Get voiceover duration
        dur_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(combined_voice)]
        total_dur = float(subprocess.check_output(dur_cmd).decode().strip()) + 0.4

        # Select background gameplay
        gameplay_files = [f for f in self.gameplay_dir.glob("*.mp4") if f.is_file()]
        if not gameplay_files:
            raise FileNotFoundError(f"No gameplay video clips found in {self.gameplay_dir}")
        chosen_gameplay = random.choice(gameplay_files)

        # Audio mixing
        mixed_audio = work_dir / "mixed_audio.wav"
        if bg_music_path and os.path.exists(bg_music_path):
            audio_cmd = [
                "ffmpeg", "-y",
                "-i", str(combined_voice),
                "-stream_loop", "-1", "-i", str(bg_music_path),
                "-filter_complex", "[0:a]volume=1.0[v];[1:a]volume=0.13[m];[v][m]amix=inputs=2:duration=first:dropout_transition=2[a]",
                "-map", "[a]",
                "-t", str(total_dur),
                str(mixed_audio)
            ]
        else:
            audio_cmd = ["ffmpeg", "-y", "-i", str(combined_voice), "-t", str(total_dur), str(mixed_audio)]

        subprocess.run(audio_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Check for hardware acceleration
        codec = "h264_nvenc"
        try:
            subprocess.run(["ffmpeg", "-f", "lavfi", "-i", "nullsrc", "-c:v", "h264_nvenc", "-t", "0.1", "-f", "null", "-"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            codec = "libx264"

        # Render final vertical video without subtitles
        render_cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-ss", "10", "-i", str(chosen_gameplay),
            "-i", str(mixed_audio),
            "-t", str(total_dur),
            "-c:v", codec,
            "-b:v", "6M" if codec == "h264_nvenc" else "4M",
            "-preset", "fast" if codec == "libx264" else "p4",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            str(output_file)
        ]
        subprocess.run(render_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(f"Dialogue short generated: {output_file} ({total_dur:.1f}s)")
        return str(output_file)
