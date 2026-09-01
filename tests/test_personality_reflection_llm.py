import unittest
from datetime import datetime

from core.experience import create_experience_slice
from core.perception import create_perception_event
from memory.experience_store import StoredExperienceSlice
from memory.personality_reflection_llm import generate_trait_candidates


def _stored(slice_id: str) -> StoredExperienceSlice:
    event = create_perception_event(
        source="user", modality="text", content=f"事件 {slice_id}",
        event_id=f"event-{slice_id}", occurred_at=datetime(2026, 8, 1).isoformat(),
    )
    experience = create_experience_slice(
        perception_event=event, perception_understanding={},
        activated_memory_refs=[], response_or_actions=[], observations=[], state_snapshot={},
    )
    return StoredExperienceSlice(experience, "thread-a", 1, event.occurred_at, 1)


class PersonalityReflectionLLMTests(unittest.TestCase):
    def test_llm_returns_candidates_without_writing(self):
        class FakeLLM:
            def invoke(self, prompt):
                self.prompt = prompt
                return type("Response", (), {"content": '[{"trait_id":"openness","direction":"increase","strength":0.2,"scope":"general","behavior_source":"autonomous_choice","evidence_slice_ids":["exp-1","exp-2","exp-3"],"counter_evidence_slice_ids":[],"confidence":0.7,"uncertainty":""}]'})()

        llm = FakeLLM()
        result = generate_trait_candidates(llm, [_stored("exp-1"), _stored("exp-2"), _stored("exp-3")])
        self.assertEqual(result[0]["trait_id"], "openness")
        self.assertIn("只输出 JSON 数组", llm.prompt)

    def test_invalid_top_level_response_is_rejected(self):
        class FakeLLM:
            def invoke(self, prompt):
                return type("Response", (), {"content": '{"trait_id":"openness"}'})()

        with self.assertRaises(ValueError):
            generate_trait_candidates(FakeLLM(), [_stored("exp-1")])


if __name__ == "__main__":
    unittest.main()
