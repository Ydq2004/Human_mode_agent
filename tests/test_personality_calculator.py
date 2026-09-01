import json
import unittest
from unittest.mock import patch

from memory.personality_calculator import (
    calculate_personality_snapshot,
    render_personality_description,
)
from memory.personality_store import TraitEvidence


class PersonalityCalculatorTests(unittest.TestCase):
    def test_missing_seed_returns_empty_snapshot(self):
        with patch("memory.personality_calculator.get_personality_seed", return_value=None):
            self.assertEqual(calculate_personality_snapshot("instance-a"), {})

    def test_snapshot_uses_seed_and_accepted_evidence(self):
        seed = {"seed_json": json.dumps({"traits": {"openness": 50, "unknown": 90}})}
        evidence = TraitEvidence(
            evidence_id="e1", trait_id="openness", direction="increase", strength=0.5,
            scope="general", behavior_source="autonomous_choice",
            evidence_slice_ids=("exp-1",), counter_evidence_slice_ids=(),
            confidence=0.8, dedupe_key="d1", algorithm_version="v1",
            created_at="2026-08-31T12:00:00", persona_instance_id="instance-a",
        )
        with (
            patch("memory.personality_calculator.get_personality_seed", return_value=seed),
            patch("memory.personality_calculator.list_trait_evidence", return_value=[evidence]),
        ):
            snapshot = calculate_personality_snapshot("instance-a")
        self.assertEqual(snapshot, {"openness": 52.4})
        self.assertIn("开放性", render_personality_description(snapshot))


if __name__ == "__main__":
    unittest.main()
