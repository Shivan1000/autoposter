"""
tests/test_dialogue_generator.py

Unit tests for DialogueShortGenerator component and presets.
"""

import unittest
from pipeline.dialogue_short_generator import DialogueShortGenerator, PRESET_SCRIPTS

class TestDialogueShortGenerator(unittest.TestCase):
    def setUp(self):
        self.generator = DialogueShortGenerator()

    def test_presets_exist(self):
        self.assertIn("instagram_bot", PRESET_SCRIPTS)
        self.assertIn("gaming_pc", PRESET_SCRIPTS)
        self.assertIn("retaining_wall", PRESET_SCRIPTS)
        self.assertIn("tornado_grandpa", PRESET_SCRIPTS)

    def test_preset_structure(self):
        for name, dialogue in PRESET_SCRIPTS.items():
            self.assertIsInstance(dialogue, list)
            self.assertGreater(len(dialogue), 5, f"Preset {name} is too short")
            for turn in dialogue:
                self.assertEqual(len(turn), 2)
                speaker, text = turn
                self.assertIn(speaker, ["delusional", "straight"])
                self.assertIsInstance(text, str)
                self.assertGreater(len(text), 0)

    def test_speaker_voice_mapping(self):
        self.assertEqual(self.generator.voice_a, "en-US-ChristopherNeural")
        self.assertEqual(self.generator.voice_b, "en-US-GuyNeural")

if __name__ == "__main__":
    unittest.main()
