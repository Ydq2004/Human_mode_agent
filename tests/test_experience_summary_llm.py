import unittest

from core.experience import create_experience_slice
from core.perception import create_perception_event
from memory.experience_store import StoredExperienceSlice
from memory.experience_summary_llm import build_deep_period_summary


class _Response:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content):
        self.content = content
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return _Response(self.content)


def _stored(slice_id: str, sequence: int) -> StoredExperienceSlice:
    event = create_perception_event(
        "user", "text", f"事件 {slice_id}", event_id=f"evt-{slice_id}",
        occurred_at="2026-08-01T10:00:00",
    )
    experience = create_experience_slice(
        perception_event=event,
        perception_understanding={},
        activated_memory_refs=[],
        response_or_actions=[],
        observations=[],
        state_snapshot={"mood": 50},
    )
    return StoredExperienceSlice(experience, "thread", sequence, "2026-08-01T10:00:01", 1)


class ExperienceSummaryLLMTests(unittest.TestCase):
    def test_llm_summary_keeps_code_owned_sources_and_metadata(self):
        llm = _FakeLLM('{"summary_text":"多次记录显示任务持续推进。","uncertainties":["关系变化未确认"]}')
        result = build_deep_period_summary(
            [_stored("one", 1)], summary_type="weekly", llm=llm,
            generated_at="2026-08-08T00:00:00",
        )
        self.assertEqual(result.source_slice_ids, (result.source_slice_ids[0],))
        self.assertEqual(result.algorithm_version, "experience-summary-llm-v1")
        self.assertEqual(result.prompt_version, "experience-summary-prompt-v1")
        self.assertIn("原始 ExperienceSlice", llm.prompts[0])

    def test_invalid_llm_output_fails_closed(self):
        llm = _FakeLLM("不是 JSON")
        with self.assertRaises(Exception):
            build_deep_period_summary(
                [_stored("one", 1)], summary_type="monthly", llm=llm
            )


if __name__ == "__main__":
    unittest.main()
