import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from emotion.appraisal_rules import (
    compute_initial_memory_impression,
    resolve_emotion_label,
)


class InitialMemoryImpressionTests(unittest.TestCase):

    def test_four_components_are_combined(self):
        result = compute_initial_memory_impression(
            impression={
                "candidate_valence": None,
                "persona_effect": "fitting",
                "direct_related_concept_ids": ["cog_coffee"],
                "label_update": {
                  "label": "开始产生兴趣",
                  "polarity": "positive",
                  "strength": "moderate",
                },
                "fallback_to_neutral": False,
            },
            event_valence="mild_positive",
            activated_memory_refs=[{
                "concept_id": "cog_coffee",
                "emotion_score": 70.0,
            }],
            mood_at_event_start=60,
        )

        # 50 + 5 + 5 + 4 + 1 = 65
        self.assertEqual(result["emotion_score"], 65.0)
        self.assertEqual(
           result["emotion_label"],
           "开始产生兴趣",
        )

    def test_missing_related_memory_creates_no_bias(self):
        result = compute_initial_memory_impression(
            impression={
                "candidate_valence": None,
                "persona_effect": "neutral",
                "direct_related_concept_ids": ["cog_fake"],
                "label_update": {
                  "label": "中性",
                  "polarity": "neutral",
                  "strength": "neutral",
                },
                "fallback_to_neutral": False,
            },
            event_valence="neutral",
            activated_memory_refs=[],
            mood_at_event_start=50,
        )

        self.assertEqual(
            result["components"]["memory_bias"],
            0.0,
        )
        self.assertEqual(result["emotion_score"], 50.0)
        self.assertEqual(result["emotion_label"], "中性")

    def test_score_is_clamped_to_initial_range(self):
        result = compute_initial_memory_impression(
            impression={
                "candidate_valence": "strong_positive",
                "persona_effect": "fitting",
                "direct_related_concept_ids": ["cog_positive"],
                "label_update": {
                                   "label": "强烈正向",
                                   "polarity": "positive",
                                   "strength": "strong",
                                },
                "fallback_to_neutral": False,
            },
            event_valence="neutral",
            activated_memory_refs=[{
                "concept_id": "cog_positive",
                "emotion_score": 100.0,
            }],
            mood_at_event_start=100,
        )

        self.assertEqual(result["emotion_score"], 70.0)
        # 70 距离中性点 20 分，应为 moderate，不能采用 strong 标签。
        self.assertEqual(result["emotion_label"], "中度正向")

    def test_explicit_fallback_returns_neutral(self):
        result = compute_initial_memory_impression(
            impression={
                "fallback_to_neutral": True,
            },
            event_valence="strong_positive",
            activated_memory_refs=[],
            mood_at_event_start=100,
        )

        self.assertEqual(result["emotion_score"], 50.0)
        self.assertTrue(result["used_fallback"])
        self.assertEqual(result["emotion_label"], "中性")

    def test_label_strength_uses_fixed_score_boundaries(self):
      cases = [
        (50.0, "中性"),
        (60.0, "轻微正向"),
        (60.01, "中度正向"),
        (75.0, "中度正向"),
        (75.01, "强烈正向"),
        (40.0, "轻微负向"),
        (24.99, "强烈负向"),
      ]

      for score, expected in cases:
        with self.subTest(score=score):
            self.assertEqual(
                resolve_emotion_label(score, None),
                expected,
            )


    def test_label_conflict_keeps_score_direction(self):
      result = resolve_emotion_label(
        65.0,
        {
            "label": "开始讨厌",
            "polarity": "negative",
            "strength": "moderate",
        },
    )

      self.assertEqual(result, "中度正向")


if __name__ == "__main__":
    unittest.main()
