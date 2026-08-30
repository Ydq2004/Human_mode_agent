import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from core.experience import create_agent_action, create_experience_slice
from core.perception import create_perception_event
from memory import experience_store, experience_summary_store
from memory.summary_scheduler import (
    generate_missing_period_summaries,
    periods_to_check,
)


def _slice(event_id: str, occurred_at: str):
    event = create_perception_event(
        source="user",
        modality="text",
        content=f"周期事件 {event_id}",
        event_id=event_id,
        occurred_at=occurred_at,
    )
    return create_experience_slice(
        perception_event=event,
        perception_understanding={},
        activated_memory_refs=[],
        response_or_actions=[create_agent_action("visible_response", "收到")],
        observations=[],
        state_snapshot={"mood": 50},
    )


class SummarySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "scheduler.db")
        self.experience_patch = patch.object(
            experience_store, "SQLITE_DB_PATH", self.db_path
        )
        self.summary_patch = patch.object(
            experience_summary_store, "SQLITE_DB_PATH", self.db_path
        )
        self.experience_patch.start()
        self.summary_patch.start()

    def tearDown(self):
        self.summary_patch.stop()
        self.experience_patch.stop()
        self.temp_dir.cleanup()

    def test_periods_are_previous_complete_day_week_and_month(self):
        periods = periods_to_check(datetime(2026, 8, 24, 12, 0, 0))
        self.assertEqual(
            [(item.summary_type, item.start_day.isoformat(), item.end_day.isoformat()) for item in periods],
            [
                ("daily", "2026-08-23", "2026-08-23"),
                ("weekly", "2026-08-17", "2026-08-23"),
                ("monthly", "2026-07-01", "2026-07-31"),
            ],
        )

    def test_scheduler_generates_and_then_skips_unchanged_periods(self):
        first = _slice("evt-1", "2026-08-23T10:00:00")
        experience_store.save_experience_slice(first, thread_id="thread-a", event_sequence=1)

        generated = generate_missing_period_summaries(
            now=datetime(2026, 8, 24, 12, 0, 0),
            thread_id="thread-a",
            generated_at="2026-08-24T12:00:00",
        )
        repeated = generate_missing_period_summaries(
            now=datetime(2026, 8, 24, 12, 0, 0),
            thread_id="thread-a",
            generated_at="2026-08-25T12:00:00",
        )

        self.assertEqual([item.summary_type for item in generated], ["daily", "weekly"])
        self.assertEqual(repeated, [])
        self.assertEqual(
            len(experience_summary_store.list_summaries(summary_type="daily")),
            1,
        )

    def test_late_slice_creates_new_version_without_deleting_old(self):
        first = _slice("evt-2", "2026-08-23T10:00:00")
        experience_store.save_experience_slice(first, thread_id="thread-a", event_sequence=1)
        generate_missing_period_summaries(now=datetime(2026, 8, 24, 12, 0, 0), thread_id="thread-a")

        late = _slice("evt-3", "2026-08-23T11:00:00")
        experience_store.save_experience_slice(late, thread_id="thread-a", event_sequence=2)
        generated = generate_missing_period_summaries(now=datetime(2026, 8, 24, 12, 0, 0), thread_id="thread-a")

        self.assertEqual([item.summary_type for item in generated], ["daily", "weekly"])
        summaries = experience_summary_store.list_summaries(summary_type="daily")
        self.assertEqual(len(summaries), 2)
        self.assertIn(
            (first.slice_id, late.slice_id),
            [item.source_slice_ids for item in summaries],
        )


if __name__ == "__main__":
    unittest.main()
