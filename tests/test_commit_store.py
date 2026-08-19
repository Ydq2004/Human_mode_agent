import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
