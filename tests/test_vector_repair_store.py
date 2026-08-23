import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory import vector_repair_store
from memory.schema import MemoryEntity


class VectorRepairStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "repair.db")
        self.path_patch = patch.object(vector_repair_store, "SQLITE_DB_PATH", self.db_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_enqueue_is_idempotent_and_refreshes_latest_failure(self):
        first = vector_repair_store.enqueue(
            "concept-1",
            "upsert",
            entity_snapshot={"summary": "旧"},
            error="第一次失败",
        )
        second = vector_repair_store.enqueue(
            "concept-1",
            "upsert",
            entity_snapshot={"summary": "新"},
            error="第二次失败",
        )

        self.assertEqual(first, second)
        pending = vector_repair_store.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["entity_snapshot"]["summary"], "新")
        self.assertEqual(pending[0]["last_error"], "第二次失败")

    def test_attempt_and_terminal_status_are_persisted(self):
        repair_id = vector_repair_store.enqueue("concept-1", "delete")
        self.assertTrue(vector_repair_store.mark_attempt(repair_id))
        self.assertEqual(
            vector_repair_store.list_pending()[0]["status"],
            "repairing",
        )
        self.assertTrue(vector_repair_store.mark_failed(repair_id, "仍失败"))
        self.assertEqual(vector_repair_store.list_pending()[0]["attempt_count"], 1)
        self.assertTrue(vector_repair_store.mark_attempt(repair_id))
        self.assertTrue(vector_repair_store.mark_repaired(repair_id))
        self.assertEqual(vector_repair_store.list_pending(), [])

    def test_repairing_task_is_recoverable_after_restart(self):
        repair_id = vector_repair_store.enqueue("concept-1", "upsert")
        self.assertTrue(vector_repair_store.mark_attempt(repair_id))
        self.assertEqual(vector_repair_store.list_pending()[0]["status"], "repairing")
        self.assertTrue(vector_repair_store.mark_attempt(repair_id))
        self.assertEqual(vector_repair_store.list_pending()[0]["attempt_count"], 2)

    def test_store_manager_records_index_failure_without_reverting_sqlite(self):
        from memory import store_manager

        entity = MemoryEntity(
            concept_id="concept-1",
            canonical_name="偏好",
            aliases=["偏好"],
            memory_type="preference",
            identity_signature={"subject": "user"},
            summary="用户喜欢水果",
        )
        with patch.object(store_manager, "create_tables"), \
             patch.object(store_manager, "sql_upsert_entity", return_value=entity), \
             patch.object(
                 store_manager,
                 "_sync_vector_index",
                 side_effect=RuntimeError("Chroma 暂时不可用"),
             ):
            with self.assertRaisesRegex(RuntimeError, "Chroma 暂时不可用"):
                store_manager.upsert_entity(entity)

        pending = vector_repair_store.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["concept_id"], "concept-1")
        self.assertEqual(pending[0]["operation"], "upsert")

    def test_repair_reads_current_sqlite_entity_instead_of_old_snapshot(self):
        from memory import store_manager

        vector_repair_store.enqueue(
            "concept-1",
            "upsert",
            entity_snapshot={"summary": "旧快照"},
            error="旧失败",
        )
        current = MemoryEntity(
            concept_id="concept-1",
            canonical_name="偏好",
            aliases=["偏好"],
            memory_type="preference",
            identity_signature={"subject": "user"},
            summary="最新摘要",
        )
        synced = []
        with patch.object(store_manager, "create_tables"), \
             patch.object(store_manager, "get_entity", return_value=current), \
             patch.object(
                 store_manager,
                 "_sync_vector_index",
                 side_effect=lambda entity: synced.append(entity.summary),
             ):
            result = store_manager.repair_pending_vectors()

        self.assertEqual(result[0]["status"], "repaired")
        self.assertEqual(synced, ["最新摘要"])
        self.assertEqual(vector_repair_store.list_pending(), [])


if __name__ == "__main__":
    unittest.main()
