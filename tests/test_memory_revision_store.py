import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from contextlib import closing

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.schema import MemoryEntity
from memory import sql_store


class MemoryRevisionStoreTests(unittest.TestCase):

    def test_existing_table_is_migrated_without_deleting_rows(self):
        with TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite")
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("""
                    CREATE TABLE memory_entities (
                        concept_id TEXT PRIMARY KEY,
                        canonical_name TEXT NOT NULL,
                        aliases TEXT NOT NULL,
                        memory_type TEXT NOT NULL,
                        identity_signature TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        tags TEXT NOT NULL,
                        emotion_score REAL NOT NULL,
                        emotion_label TEXT NOT NULL,
                        mention_count INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        last_accessed_at TEXT NOT NULL,
                        last_modified_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        confidence REAL NOT NULL
                    )
                """)
                conn.execute("""
                    INSERT INTO memory_entities VALUES (
                        'old', '旧认知', '[]', 'preference', '{}',
                        '旧摘要', '[]', 50, '中性', 1,
                        '', '', '', 'user_told', 1.0
                    )
                """)
                conn.commit()

            with patch.object(sql_store, "SQLITE_DB_PATH", db_path):
                sql_store.create_tables()
                migrated = sql_store.get_entity("old")

            self.assertIsNotNone(migrated)
            self.assertEqual(migrated.summary, "旧摘要")
            self.assertEqual(migrated.revision, 1)

    def test_content_update_requires_matching_revision(self):
        with TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.sqlite")
            with patch.object(sql_store, "SQLITE_DB_PATH", db_path):
                sql_store.create_tables()
                entity = MemoryEntity(
                    concept_id="coffee",
                    canonical_name="咖啡偏好",
                    aliases=[],
                    memory_type="preference",
                    identity_signature={},
                    summary="用户喜欢甜咖啡。",
                )
                sql_store.upsert_entity(entity)

                stale = sql_store.get_entity("coffee")
                stale.summary = "旧版本错误覆盖。"
                self.assertIsNone(sql_store.update_entity_content(
                    stale,
                    expected_revision=2,
                ))

                current = sql_store.get_entity("coffee")
                current.summary = "用户现在偏好无糖咖啡。"
                saved = sql_store.update_entity_content(
                    current,
                    expected_revision=1,
                )

            self.assertEqual(saved.revision, 2)
            self.assertEqual(saved.summary, "用户现在偏好无糖咖啡。")


if __name__ == "__main__":
    unittest.main()
