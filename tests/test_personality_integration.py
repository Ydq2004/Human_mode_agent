import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from core.experience import create_experience_slice
from core.perception import create_perception_event
from memory import experience_store, personality_store
from memory.personality_calculator import calculate_personality_snapshot
from memory.personality_store import TraitEvidence, append_trait_evidence, save_personality_seed
from memory.personality_validator import validate_trait_evidence


class PersonalityIntegrationTests(unittest.TestCase):
    def test_validated_evidence_changes_snapshot_without_touching_experience(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "personality.db")
            with (
                patch.object(experience_store, "SQLITE_DB_PATH", db_path),
                patch.object(personality_store, "SQLITE_DB_PATH", db_path),
            ):
                slices = []
                for index, day in enumerate((0, 2, 4), start=1):
                    event = create_perception_event(
                        source="user", modality="text", content=f"主动行为 {index}",
                        event_id=f"event-{index}",
                        occurred_at=(datetime(2026, 8, 1) + timedelta(days=day)).isoformat(),
                    )
                    item = create_experience_slice(
                        perception_event=event, perception_understanding={},
                        activated_memory_refs=[], response_or_actions=[],
                        observations=[], state_snapshot={}
                    )
                    experience_store.save_experience_slice(item, thread_id="thread-a", event_sequence=index)
                    slices.append(item)

                save_personality_seed(
                    seed_id="persona-a", persona_instance_id="persona-a",
                    persona_name="Agent", seed={"traits": {"openness": 50}},
                    source_persona_hash="hash-a",
                )
                candidate = {
                    "trait_id": "openness", "direction": "increase", "strength": 0.5,
                    "scope": "general", "behavior_source": "autonomous_choice",
                    "evidence_slice_ids": [item.slice_id for item in slices],
                    "counter_evidence_slice_ids": [], "confidence": 0.8,
                }
                validated = validate_trait_evidence(candidate, persona_instance_id="persona-a")
                evidence = TraitEvidence(
                    evidence_id="evidence-1", created_at="2026-08-06T00:00:00",
                    dedupe_key="openness:exp-1:exp-2:exp-3",
                    algorithm_version="trait-evidence-v1", **validated
                )
                append_trait_evidence(evidence)
                snapshot = calculate_personality_snapshot("persona-a")

                self.assertEqual(snapshot["openness"], 52.4)
                self.assertEqual(len(experience_store.list_experience_slices(thread_id="thread-a")), 3)


if __name__ == "__main__":
    unittest.main()
