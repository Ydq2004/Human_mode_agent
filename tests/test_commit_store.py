import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from memory import commit_store


class CommitStoreTests(unittest.TestCase):
    def test_appraisal_and_commit_status_survive_new_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "ledger.db")
            job = {
                "job_id": "appraisal_exp_1",
                "experience_slice_id": "exp_1",
                "event_id": "evt_1",
                "event_sequence": 1,
                "thread_id": "thread-1",
                "status": "completed",
                "appraisal": {"memory_assessment": {"memory_candidates": []}},
                "effects": {"mood": {"mood_impact": 0}},
                "submitted_at": "2026-08-19T10:00:00",
                "completed_at": "2026-08-19T10:00:01",
                "error": None,
            }

            with patch.object(commit_store, "SQLITE_DB_PATH", db_path):
                commit_store.record_appraisal_terminal(job)
                self.assertEqual(
                    len(commit_store.list_unfinished_commits()),
                    1,
                )
                commit_store.mark_commit_started(job["job_id"])
                commit_store.mark_commit_terminal(
                    job["job_id"],
                    status="committed",
                    result={"written": True},
                )
                self.assertEqual(commit_store.list_unfinished_commits(), [])

    def test_repeating_same_terminal_result_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "ledger.db")
            job = {
                "job_id": "appraisal_exp_2",
                "experience_slice_id": "exp_2",
                "event_id": "evt_2",
                "event_sequence": 2,
                "thread_id": "thread-2",
                "status": "failed",
                "appraisal": None,
                "effects": None,
                "submitted_at": "2026-08-19T10:00:00",
                "completed_at": "2026-08-19T10:00:01",
                "error": "temporary failure",
            }

            with patch.object(commit_store, "SQLITE_DB_PATH", db_path):
                commit_store.record_appraisal_terminal(job)
                commit_store.record_appraisal_terminal(job)
                self.assertEqual(
                    len(commit_store.list_unfinished_commits()),
                    1,
                )

    def test_recoverable_commits_only_returns_recent_completed_waiting_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "ledger.db")
            now = datetime(2026, 8, 30, 12, 0, 0)
            base = {
                "experience_slice_id": "exp",
                "event_id": "evt",
                "event_sequence": 1,
                "thread_id": "thread-1",
                "status": "completed",
                "appraisal": {"memory_assessment": {"memory_candidates": []}},
                "effects": {"mood": {"mood_impact": 0}},
                "submitted_at": "2026-08-30T11:00:00",
                "completed_at": "2026-08-30T11:00:01",
            }

            with patch.object(commit_store, "SQLITE_DB_PATH", db_path):
                commit_store.record_appraisal_terminal(
                    {**base, "job_id": "recoverable"}
                )
                commit_store.record_appraisal_terminal(
                    {
                        **base,
                        "job_id": "failed-appraisal",
                        "status": "failed",
                        "appraisal": None,
                        "effects": None,
                    }
                )
                commit_store.record_appraisal_terminal(
                    {
                        **base,
                        "job_id": "old",
                        "submitted_at": "2026-08-28T11:00:00",
                    }
                )

                rows = commit_store.list_recoverable_commits(
                    thread_id="thread-1",
                    max_age_hours=24,
                    now=now,
                )
                self.assertEqual([row["job_id"] for row in rows], ["recoverable"])
                self.assertIsInstance(rows[0]["appraisal_json"], dict)
                self.assertIsInstance(rows[0]["effects_json"], dict)

    def test_mark_abandoned_is_explicit_and_keeps_record(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "ledger.db")
            job = {
                "job_id": "waiting-job",
                "experience_slice_id": "exp-3",
                "event_id": "evt-3",
                "event_sequence": 3,
                "thread_id": "thread-3",
                "status": "completed",
                "appraisal": {},
                "effects": {},
                "submitted_at": "2026-08-30T10:00:00",
                "completed_at": "2026-08-30T10:00:01",
            }
            with patch.object(commit_store, "SQLITE_DB_PATH", db_path):
                commit_store.record_appraisal_terminal(job)
                self.assertTrue(
                    commit_store.mark_commit_abandoned(
                        job["job_id"], "缺少可恢复的原始输入"
                    )
                )
                self.assertEqual(commit_store.list_unfinished_commits(), [])
                self.assertFalse(
                    commit_store.mark_commit_abandoned(job["job_id"], "再次处理")
                )


if __name__ == "__main__":
    unittest.main()
