import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.somatic_simulation import SomaticState
from memory import somatic_store


class SomaticStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "somatic.db")
        self.db_patch = patch.object(somatic_store, "SQLITE_DB_PATH", self.db_path)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def _commit(self, **overrides):
        values = {
            "operation_id": "op-1",
            "persona_instance_id": "persona-a",
            "thread_id": "thread-a",
            "source_event_id": "evt-1",
            "source_kind": "user_contact",
            "expected_revision": 0,
            "state_after": SomaticState({"arousal": 0.55, "sensitivity": 0.5}),
            "decision": {"status": "occurred"},
            "appraisal": {"arousal_change": "increase"},
            "result": {"visible_reaction": "轻轻缩了一下手"},
        }
        values.update(overrides)
        return somatic_store.commit_somatic_transition(**values)

    def test_default_state_is_persisted_and_isolated(self):
        first = somatic_store.get_somatic_state("persona-a", "thread-a")
        other = somatic_store.get_somatic_state("persona-a", "thread-b")

        self.assertEqual(first.state.to_dict(), {"arousal": 0.5, "sensitivity": 0.5})
        self.assertEqual(first.revision, 0)
        self.assertEqual(other.thread_id, "thread-b")

    def test_transition_survives_a_new_connection(self):
        result = self._commit()
        restored = somatic_store.get_somatic_state("persona-a", "thread-a")

        self.assertEqual(result.status, "committed")
        self.assertEqual(restored.state.values["arousal"], 0.55)
        self.assertEqual(restored.revision, 1)

    def test_operation_id_is_idempotent(self):
        first = self._commit()
        repeated = self._commit(
            expected_revision=1,
            state_after=SomaticState({"arousal": 0.9, "sensitivity": 0.9}),
        )

        self.assertFalse(first.duplicate)
        self.assertTrue(repeated.duplicate)
        self.assertEqual(repeated.record.state.values["arousal"], 0.55)
        self.assertEqual(repeated.record.revision, 1)

    def test_stale_revision_cannot_overwrite_state(self):
        self._commit()
        conflict = self._commit(
            operation_id="op-2",
            source_event_id="evt-2",
            expected_revision=0,
            state_after=SomaticState({"arousal": 0.1, "sensitivity": 0.1}),
        )

        self.assertEqual(conflict.status, "revision_conflict")
        self.assertEqual(conflict.record.state.values["arousal"], 0.55)
        self.assertIsNone(somatic_store.get_somatic_operation("op-2"))


if __name__ == "__main__":
    unittest.main()
