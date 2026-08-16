import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from emotion.appraisal_rules import (
    compute_mood_impact,
    round_half_away_from_zero,
)


class MoodAppraisalRulesTests(unittest.TestCase):

    def test_round_half_away_from_zero(self):
        self.assertEqual(round_half_away_from_zero(0.5), 1)
        self.assertEqual(round_half_away_from_zero(-0.5), -1)

    def test_reactivity_can_reach_clamp(self):
        result = compute_mood_impact(
            {
                "event_relevance": "high",
                "event_valence": "strong_positive",
                "salience": "high",
            },
            current_mood=50,
            mood_reactivity=1.5,
        )

        self.assertEqual(result["raw_impact"], 12.0)
        self.assertEqual(result["mood_impact"], 10)
        self.assertTrue(result["was_clipped"])

    def test_low_relevance_cannot_change_mood(self):
        result = compute_mood_impact(
            {
                "event_relevance": "low",
                "event_valence": "strong_positive",
                "salience": "high",
            },
            current_mood=50,
            mood_reactivity=1.5,
        )

        self.assertEqual(result["mood_impact"], 0)

    def test_boundary_damping_reduces_positive_impact(self):
        result = compute_mood_impact(
            {
                "event_relevance": "high",
                "event_valence": "strong_positive",
                "salience": "high",
            },
            current_mood=98,
            mood_reactivity=1.0,
        )

        self.assertLess(result["mood_impact"], 4)


if __name__ == "__main__":
    unittest.main()
