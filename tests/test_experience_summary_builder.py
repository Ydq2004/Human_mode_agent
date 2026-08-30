import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.experience import create_agent_action, create_experience_slice
from core.perception import create_perception_event
from memory.experience_store import StoredExperienceSlice
from memory.experience_summary_builder import (
    build_summary,
    build_daily_summary,
    build_segment_summary,
)


def _stored(event_id: str, occurred_at: str, content: str) -> StoredExperienceSlice:
    event = create_perception_event(
        source="user",
        modality="text",
        content=content,
        event_id=event_id,
        occurred_at=occurred_at,
    )
    experience = create_experience_slice(
        perception_event=event,
        perception_understanding={
            "uncertainties": ["是否需要继续追问"],
        },
        activated_memory_refs=[],
        response_or_actions=[
            create_agent_action("visible_response", "我听到了"),
        ],
        observations=[],
        state_snapshot={"mood": 50},
        capability_snapshot={},
        memory_activation_state={},
    )
    return StoredExperienceSlice(
        experience=experience,
        thread_id="thread-a",
        event_sequence=int(event_id.rsplit("-", 1)[-1]),
        stored_at=occurred_at,
        schema_version=1,
    )


class ExperienceSummaryBuilderTests(unittest.TestCase):
    def test_segment_summary_is_stable_and_keeps_source_order(self):
        first = _stored("evt-1", "2026-08-24T10:00:00", "先发生的事件")
        second = _stored("evt-2", "2026-08-24T10:01:00", "后发生的事件")

        summary = build_segment_summary(
            [second, first],
            generated_at="2026-08-24T10:02:00",
        )

        self.assertEqual(
            summary.source_slice_ids,
            (first.experience.slice_id, second.experience.slice_id),
        )
        self.assertLess(
            summary.summary_text.index("先发生的事件"),
            summary.summary_text.index("后发生的事件"),
        )
        self.assertIn("不据此判断关系发展、人格变化或情绪结论", summary.uncertainties[0])
        self.assertIsNone(summary.prompt_version)

    def test_daily_summary_has_range_and_does_not_mutate_slice(self):
        item = _stored("evt-3", "2026-08-24T12:00:00", "原始消息")
        before = item.experience.to_dict()

        summary = build_daily_summary([item], text_limit=20)

        self.assertEqual(summary.summary_type, "daily")
        self.assertEqual(summary.start_at, "2026-08-24T12:00:00")
        self.assertEqual(summary.end_at, "2026-08-24T12:00:00")
        self.assertEqual(item.experience.to_dict(), before)
        self.assertIn("原始消息", summary.summary_text)

    def test_empty_input_is_rejected(self):
        with self.assertRaises(ValueError):
            build_segment_summary([])

    def test_daily_summary_rejects_multiple_dates(self):
        with self.assertRaises(ValueError):
            build_daily_summary(
                [
                    _stored("evt-5", "2026-08-24T23:59:00", "前一天"),
                    _stored("evt-6", "2026-08-25T00:01:00", "下一天"),
                ]
            )


if __name__ == "__main__":
    unittest.main()
