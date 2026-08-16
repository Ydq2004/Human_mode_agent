import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from emotion.appraisal_rules import (
    compute_existing_memory_emotion_update,
)


class ExistingMemoryEmotionTests(unittest.TestCase):

    def test_strengthened_adds_two(self):
        result = compute_existing_memory_emotion_update(
            50.0,
            {
                "change_direction": "strengthened",
                "label_update": {
                    "label": "有些喜欢",
                    "polarity": "positive",
                    "strength": "slight",
                },
            },
        )

        self.assertEqual(result["emotion_score"], 52.0)
        self.assertEqual(result["score_delta"], 2.0)
        self.assertEqual(result["emotion_label"], "有些喜欢")

    def test_slightly_positive_adds_one(self):
        result = compute_existing_memory_emotion_update(
            50.0,
            {
                "change_direction": "slightly_positive",
                "label_update": None,
            },
        )

        self.assertEqual(result["emotion_score"], 51.0)
        self.assertEqual(result["emotion_label"], "轻微正向")

    def test_negative_direction_subtracts(self):
        result = compute_existing_memory_emotion_update(
            50.0,
            {
                "change_direction": "weakened",
                "label_update": None,
            },
        )

        self.assertEqual(result["emotion_score"], 48.0)
        self.assertEqual(result["emotion_label"], "轻微负向")

    def test_unchanged_keeps_score_and_label(self):
        result = compute_existing_memory_emotion_update(
            65.0,
            {
                "change_direction": "unchanged",
                "label_update": None,
            },
        )

        self.assertEqual(result["emotion_score"], 65.0)
        self.assertEqual(result["score_delta"], 0.0)
        self.assertEqual(result["emotion_label"], "中度正向")

    def test_score_is_clamped_at_boundaries(self):
        high = compute_existing_memory_emotion_update(
            99.0,
            {
                "change_direction": "strengthened",
                "label_update": None,
            },
        )
        low = compute_existing_memory_emotion_update(
            1.0,
            {
                "change_direction": "weakened",
                "label_update": None,
            },
        )

        self.assertEqual(high["emotion_score"], 100.0)
        self.assertEqual(low["emotion_score"], 0.0)

    def test_conflicting_label_cannot_change_score_direction(self):
        result = compute_existing_memory_emotion_update(
            50.0,
            {
                "change_direction": "slightly_positive",
                "label_update": {
                    "label": "开始讨厌",
                    "polarity": "negative",
                    "strength": "slight",
                },
            },
        )

        self.assertEqual(result["emotion_score"], 51.0)
        self.assertEqual(result["emotion_label"], "轻微正向")


if __name__ == "__main__":
    unittest.main()