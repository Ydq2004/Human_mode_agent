import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from core.experience import create_experience_slice
from core.perception import create_perception_event
from main import _prepare_appraisal_recovery, _prepare_commit_recovery
from memory import commit_store


class CommitRecoveryTests(unittest.TestCase):
    def test_pending_appraisal_plan_rebuilds_from_persisted_input(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "ledger.db")
            event = create_perception_event("user", "text", "待评价恢复")
            experience = create_experience_slice(
                perception_event=event,
                perception_understanding={},
                activated_memory_refs=[],
                response_or_actions=[],
                observations=[],
                state_snapshot={"mood": 50},
            )
            with (
                patch.object(commit_store, "SQLITE_DB_PATH", db_path),
                patch("main.get_experience_slice", return_value=type("Stored", (), {"experience": experience})()),
            ):
                commit_store.record_appraisal_pending(
                    job_id="appraisal_pending",
                    experience_slice_id=experience.slice_id,
                    event_id=event.event_id,
                    event_sequence=1,
                    thread_id="thread-pending",
                    persona_context={"agent_name": "Agent"},
                    mood_reactivity=1.0,
                )
                rows, notes = _prepare_appraisal_recovery(thread_id="thread-pending")

            self.assertEqual(notes, [])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["mood_reactivity"], 1.0)
            self.assertEqual(rows[0]["experience"].slice_id, experience.slice_id)

    def test_recovery_plan_rebuilds_unique_waiting_task_and_skips_terminal_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "ledger.db")
            now = datetime.now().isoformat()
            event = create_perception_event("user", "text", "测试恢复")
            experience = create_experience_slice(
                perception_event=event,
                perception_understanding={},
                activated_memory_refs=[],
                response_or_actions=[],
                observations=[],
                state_snapshot={"mood": 50},
                capability_snapshot={},
                memory_activation_state={},
                preceding_context={},
            )
            base = {
                "experience_slice_id": experience.slice_id,
                "event_id": event.event_id,
                "event_sequence": 2,
                "thread_id": "thread-recovery",
                "status": "completed",
                "appraisal": {"memory_assessment": {"memory_candidates": []}},
                "effects": {"mood": {"mood_impact": 0}},
                "event_evidence": {"perception_event": event.to_dict()},
                "submitted_at": now,
                "completed_at": now,
            }

            with (
                patch.object(commit_store, "SQLITE_DB_PATH", db_path),
                patch("main.get_experience_slice", return_value=type("Stored", (), {"experience": experience})()),
                patch("main.COMMIT_RECOVERY_MAX_AGE_HOURS", 24.0),
            ):
                commit_store.record_appraisal_terminal({**base, "job_id": "waiting-2"})
                commit_store.record_appraisal_terminal(
                    {
                        **base,
                        "job_id": "done-1",
                        "event_sequence": 1,
                        "experience_slice_id": "other-slice",
                        "event_id": "other-event",
                    }
                )
                commit_store.mark_commit_started("done-1")
                commit_store.mark_commit_terminal("done-1", status="committed", result={})

                tasks, start, processed, notes = _prepare_commit_recovery(
                    thread_id="thread-recovery",
                    next_event_sequence=3,
                )

            self.assertEqual([task.job_id for task in tasks], ["waiting-2"])
            self.assertEqual(start, 2)
            self.assertEqual(processed, {1})
            self.assertEqual(notes, [])
            self.assertEqual(tasks[0].appraisal_job["event_evidence"]["perception_event"]["content"], "测试恢复")

    def test_processed_sequence_between_waiting_rows_does_not_block_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "ledger.db")
            now = datetime.now().isoformat()
            event = create_perception_event("user", "text", "连续性测试")
            experience = create_experience_slice(
                perception_event=event,
                perception_understanding={},
                activated_memory_refs=[],
                response_or_actions=[],
                observations=[],
                state_snapshot={"mood": 50},
                capability_snapshot={},
                memory_activation_state={},
                preceding_context={},
            )
            base = {
                "experience_slice_id": experience.slice_id,
                "event_id": event.event_id,
                "thread_id": "thread-contiguous",
                "status": "completed",
                "appraisal": {"memory_assessment": {"memory_candidates": []}},
                "effects": {"mood": {"mood_impact": 0}},
                "submitted_at": now,
                "completed_at": now,
            }
            with (
                patch.object(commit_store, "SQLITE_DB_PATH", db_path),
                patch("main.get_experience_slice", return_value=type("Stored", (), {"experience": experience})()),
                patch("main.COMMIT_RECOVERY_MAX_AGE_HOURS", 24.0),
            ):
                for sequence in (2, 3, 5, 6):
                    commit_store.record_appraisal_terminal(
                        {**base, "job_id": f"waiting-{sequence}", "event_id": f"evt-{sequence}", "event_sequence": sequence}
                    )
                commit_store.record_appraisal_terminal(
                    {**base, "job_id": "committed-4", "event_id": "evt-4", "event_sequence": 4}
                )
                commit_store.mark_commit_started("committed-4")
                commit_store.mark_commit_terminal("committed-4", status="committed", result={})

                tasks, start, processed, notes = _prepare_commit_recovery(
                    thread_id="thread-contiguous",
                    next_event_sequence=7,
                )

            self.assertEqual([task.event_sequence for task in tasks], [2, 3, 5, 6])
            self.assertEqual(start, 2)
            self.assertEqual(processed, {4})
            self.assertEqual(notes, [])

    def test_missing_experience_is_marked_abandoned(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "ledger.db")
            now = datetime.now().isoformat()
            job = {
                "job_id": "missing-slice",
                "experience_slice_id": "does-not-exist",
                "event_id": "evt-missing",
                "event_sequence": 1,
                "thread_id": "thread-missing",
                "status": "completed",
                "appraisal": {"memory_assessment": {"memory_candidates": []}},
                "effects": {"mood": {"mood_impact": 0}},
                "submitted_at": now,
                "completed_at": now,
            }
            with (
                patch.object(commit_store, "SQLITE_DB_PATH", db_path),
                patch("main.get_experience_slice", return_value=None),
                patch("main.COMMIT_RECOVERY_MAX_AGE_HOURS", 24.0),
            ):
                commit_store.record_appraisal_terminal(job)
                tasks, start, processed, notes = _prepare_commit_recovery(
                    thread_id="thread-missing",
                    next_event_sequence=2,
                )
                rows = commit_store.list_commit_records(thread_id="thread-missing")

            self.assertEqual(tasks, [])
            self.assertEqual(start, 2)
            self.assertEqual(processed, {1})
            self.assertIn("已标记 abandoned", notes[0])
            self.assertEqual(rows[0]["commit_status"], "abandoned")
            self.assertIn("ExperienceSlice", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
