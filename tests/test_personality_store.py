import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory import personality_store


class PersonalityStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "personality.db")
        self.patch = patch.object(personality_store, "SQLITE_DB_PATH", self.db_path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp_dir.cleanup()

    def test_seed_is_write_once(self):
        first = personality_store.save_personality_seed(
            seed_id="seed-default",
            persona_name="Agent",
            seed={"personality": "冷静"},
            source_persona_hash="hash-a",
        )
        second = personality_store.save_personality_seed(
            seed_id="seed-default",
            persona_name="Other",
            seed={"personality": "热情"},
            source_persona_hash="hash-b",
        )
        self.assertEqual(first["persona_name"], "Agent")
        self.assertEqual(second["seed_json"], first["seed_json"])

    def test_trait_evidence_is_append_only_and_idempotent(self):
        evidence = personality_store.TraitEvidence(
            evidence_id="trait-1",
            trait_id="initiative",
            direction="increase",
            strength=0.4,
            scope="user_interaction",
            behavior_source="autonomous_choice",
            evidence_slice_ids=("exp-1", "exp-2"),
            counter_evidence_slice_ids=("exp-3",),
            confidence=0.8,
            dedupe_key="initiative:exp-1:exp-2",
            algorithm_version="trait-evidence-v1",
            created_at="2026-08-31T12:00:00",
        )
        first = personality_store.append_trait_evidence(evidence)
        second = personality_store.append_trait_evidence(evidence)
        rows = personality_store.list_trait_evidence(trait_id="initiative")
        self.assertEqual(first, second)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].evidence_slice_ids, ("exp-1", "exp-2"))

    def test_trait_evidence_requires_source_slice(self):
        with self.assertRaises(ValueError):
            personality_store.TraitEvidence(
                evidence_id="trait-invalid",
                trait_id="initiative",
                direction="increase",
                strength=0.4,
                scope="global",
                behavior_source="unknown",
                evidence_slice_ids=(),
                counter_evidence_slice_ids=(),
                confidence=0.5,
                dedupe_key="invalid",
                algorithm_version="trait-evidence-v1",
                created_at="2026-08-31T12:00:00",
            )

    def test_reflection_run_records_scope_and_latest_completed_run(self):
        personality_store.record_reflection_run(
            reflection_id="reflection-1",
            persona_instance_id="persona-a",
            status="running",
            this_run_start_sequence=10,
            this_run_end_sequence=20,
            started_at="2026-08-31T12:00:00",
        )
        personality_store.record_reflection_run(
            reflection_id="reflection-1",
            persona_instance_id="persona-a",
            status="completed",
            last_analyzed_sequence=20,
            this_run_start_sequence=10,
            this_run_end_sequence=20,
            started_at="2026-08-31T12:00:00",
            completed_at="2026-08-31T12:01:00",
            accepted_evidence_count=2,
        )
        latest = personality_store.get_last_reflection_run("persona-a")
        self.assertEqual(latest["last_analyzed_sequence"], 20)
        self.assertEqual(latest["accepted_evidence_count"], 2)
        self.assertEqual(latest["persona_instance_id"], "persona-a")
        self.assertEqual(latest["thread_id"], None)

    def test_personality_snapshot_is_append_only_and_idempotent(self):
        first = personality_store.save_personality_snapshot(
            persona_instance_id="persona-a",
            trait_values={"openness": 50.0},
            as_of_timestamp="2026-08-31T12:00:00",
            created_at="2026-08-31T12:01:00",
        )
        second = personality_store.save_personality_snapshot(
            persona_instance_id="persona-a",
            trait_values={"openness": 50.0},
            as_of_timestamp="2026-08-31T12:00:00",
            created_at="2026-08-31T12:02:00",
        )
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(len(personality_store.list_personality_snapshots("persona-a")), 1)
        self.assertEqual(personality_store.get_latest_personality_snapshot("persona-a")["trait_values"]["openness"], 50.0)


if __name__ == "__main__":
    unittest.main()
