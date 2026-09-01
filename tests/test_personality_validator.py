import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from core.experience import create_experience_slice
from core.perception import create_perception_event
from memory.experience_store import StoredExperienceSlice
from memory.personality_validator import (
    EvidenceValidationError,
    validate_reflection_batch,
    validate_trait_evidence,
)


def _stored(slice_id: str, day: int) -> StoredExperienceSlice:
    event = create_perception_event(
        source="user", modality="text", content=slice_id,
        event_id=f"event-{slice_id}",
        occurred_at=(datetime(2026, 8, 1) + timedelta(days=day)).isoformat(),
    )
    experience = create_experience_slice(
        perception_event=event,
        perception_understanding={}, activated_memory_refs=[],
        response_or_actions=[], observations=[], state_snapshot={},
    )
    return StoredExperienceSlice(experience, "instance-a", day + 1, event.occurred_at, 1)


class PersonalityValidatorTests(unittest.TestCase):
    def setUp(self):
        self.rows = {f"exp-{i}": _stored(f"exp-{i}", i) for i in range(5)}

    def _candidate(self, **updates):
        value = {
            "trait_id": "openness", "direction": "increase", "strength": 0.4,
            "scope": "task_handling", "behavior_source": "autonomous_choice",
            "evidence_slice_ids": ["exp-0", "exp-2", "exp-4"],
            "counter_evidence_slice_ids": [], "confidence": 0.8,
        }
        value.update(updates)
        return value

    def test_valid_candidate_is_accepted(self):
        with patch("memory.personality_validator.get_experience_slice", side_effect=self.rows.get):
            result = validate_trait_evidence(self._candidate(), persona_instance_id="persona-a")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["confidence"], 0.8)

    def test_insufficient_evidence_is_rejected(self):
        with patch("memory.personality_validator.get_experience_slice", side_effect=self.rows.get):
            with self.assertRaises(EvidenceValidationError):
                validate_trait_evidence(self._candidate(evidence_slice_ids=["exp-0", "exp-1"]))

    def test_counter_evidence_reduces_confidence(self):
        with patch("memory.personality_validator.get_experience_slice", side_effect=self.rows.get):
            result = validate_trait_evidence(
                self._candidate(counter_evidence_slice_ids=["exp-1"]),
                persona_instance_id="persona-a",
            )
        self.assertLess(result["confidence"], 0.8)

    def test_counter_evidence_that_is_too_strong_is_rejected(self):
        with patch("memory.personality_validator.get_experience_slice", side_effect=self.rows.get):
            with self.assertRaises(EvidenceValidationError):
                validate_trait_evidence(
                    self._candidate(counter_evidence_slice_ids=["exp-1", "exp-3", "exp-4"])
                )

    def test_batch_rejects_excessive_single_reflection_change(self):
        candidate = self._candidate(strength=1.0, confidence=1.0)
        with patch("memory.personality_validator.get_experience_slice", side_effect=self.rows.get):
            valid, rejected = validate_reflection_batch([candidate])
        self.assertEqual(valid, [])
        self.assertTrue(any("变化过大" in item for item in rejected))


if __name__ == "__main__":
    unittest.main()
