import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.experience import create_agent_action, create_experience_slice
from core.perception import create_perception_event
from memory import experience_store, experience_summary_store
from memory.experience_summary_pipeline import generate_daily_summary


def _slice(event_id: str, occurred_at: str):
    event = create_perception_event(
        source="user",
        modality="text",
        content=f"当天事件 {event_id}",
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


class ExperienceSummaryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "summary_pipeline.db")
        self.experience_patch = patch.object(
            experience_store,
            "SQLITE_DB_PATH",
            self.db_path,
        )
        self.summary_patch = patch.object(
            experience_summary_store,
            "SQLITE_DB_PATH",
            self.db_path,
        )
        self.experience_patch.start()
        self.summary_patch.start()

    def tearDown(self):
        self.summary_patch.stop()
        self.experience_patch.stop()
        self.temp_dir.cleanup()

    def test_generates_daily_summary_from_that_day_and_is_idempotent(self):
        first = _slice("evt-day-1", "2026-08-24T10:00:00")
        next_day = _slice("evt-day-2", "2026-08-25T10:00:00")
        experience_store.save_experience_slice(first, thread_id="thread-a", event_sequence=1)
        experience_store.save_experience_slice(next_day, thread_id="thread-a", event_sequence=2)

        summary = generate_daily_summary(
            "2026-08-24",
            thread_id="thread-a",
            generated_at="2026-08-25T00:00:00",
        )
        repeated = generate_daily_summary(
            "2026-08-24",
            thread_id="thread-a",
            generated_at="2026-08-26T00:00:00",
        )

        self.assertIsNotNone(summary)
        self.assertEqual(summary, repeated)
        self.assertEqual(summary.source_slice_ids, (first.slice_id,))
        self.assertEqual(
            len(experience_summary_store.list_summaries(summary_type="daily")),
            1,
        )

    def test_empty_day_returns_none_and_invalid_day_is_rejected(self):
        self.assertIsNone(generate_daily_summary("2026-08-24"))
        with self.assertRaises(ValueError):
            generate_daily_summary("2026/08/24")

    def test_thread_filter_keeps_other_thread_out(self):
        first = _slice("evt-thread-1", "2026-08-24T10:00:00")
        second = _slice("evt-thread-2", "2026-08-24T11:00:00")
        experience_store.save_experience_slice(first, thread_id="thread-a", event_sequence=1)
        experience_store.save_experience_slice(second, thread_id="thread-b", event_sequence=1)

        summary = generate_daily_summary("2026-08-24", thread_id="thread-a")

        self.assertEqual(summary.source_slice_ids, (first.slice_id,))


if __name__ == "__main__":
    unittest.main()
