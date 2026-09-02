import tempfile
import unittest
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from core.experience import create_experience_slice
from core.perception import create_perception_event
from memory import experience_store, personality_store
from memory.personality_reflection_runner import run_personality_reflection


class _FakeLLM:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return type("Response", (), {"content": self.content})()


def _make_slices(count=3, start_index=0, thread_id="thread-a"):
    result = []
    for index in range(count):
        event = create_perception_event(
            source="user", modality="text", content=f"长期主动行为 {index}",
            event_id=f"runner-event-{start_index + index}",
            occurred_at=(datetime(2026, 8, 1) + timedelta(days=(start_index + index) * 2)).isoformat(),
        )
        item = create_experience_slice(
            perception_event=event,
            perception_understanding={"situated_understanding": "测试"},
            activated_memory_refs=[], response_or_actions=[], observations=[], state_snapshot={},
        )
        experience_store.save_experience_slice(item, thread_id=thread_id, event_sequence=start_index + index + 1)
        result.append(item)
    return result


class PersonalityReflectionRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "runner.db")
        self.patches = [
            patch.object(experience_store, "SQLITE_DB_PATH", self.db_path),
            patch.object(personality_store, "SQLITE_DB_PATH", self.db_path),
        ]
        for item in self.patches:
            item.start()
        personality_store.save_personality_seed(
            seed_id="persona-a", persona_instance_id="persona-a", persona_name="Agent",
            seed={"traits": {"openness": 50}}, source_persona_hash="hash-a",
        )

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def test_insufficient_new_slices_does_not_call_llm(self):
        _make_slices(2)
        llm = _FakeLLM("[]")
        result = run_personality_reflection(
            persona_instance_id="persona-a", thread_id="thread-a", llm=llm,
            min_new_slices=3, min_interval_days=0,
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "insufficient_new_slices")
        self.assertEqual(llm.calls, 0)

    def test_thread_scope_is_required(self):
        with self.assertRaises(ValueError):
            run_personality_reflection(
                persona_instance_id="persona-a", llm=_FakeLLM("[]"),
                min_new_slices=1, min_interval_days=0,
            )

    def test_success_validates_and_saves_evidence(self):
        slices = _make_slices(3)
        content = (
            '[{"trait_id":"openness","direction":"increase","strength":0.5,'
            '"scope":"general","behavior_source":"autonomous_choice",'
            f'"evidence_slice_ids":["{slices[0].slice_id}","{slices[1].slice_id}","{slices[2].slice_id}"],'
            '"counter_evidence_slice_ids":[],"confidence":0.8,"uncertainty":""}]'
        )
        llm = _FakeLLM(content)
        result = run_personality_reflection(
            persona_instance_id="persona-a", thread_id="thread-a", llm=llm,
            min_new_slices=3, min_interval_days=0,
            now="2026-08-10T00:00:00",
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(len(personality_store.list_trait_evidence(persona_instance_id="persona-a")), 1)
        self.assertEqual(personality_store.get_last_reflection_run("persona-a", thread_id="thread-a")["last_analyzed_sequence"], 3)
        snapshot = personality_store.get_latest_personality_snapshot("persona-a")
        self.assertEqual(snapshot["trait_values"]["openness"], 52.4)

        again = run_personality_reflection(
            persona_instance_id="persona-a", thread_id="thread-a", llm=llm,
            min_new_slices=3, min_interval_days=0,
            now="2026-08-11T00:00:00",
        )
        self.assertEqual(again["reason"], "no_new_slices")
        self.assertEqual(llm.calls, 1)

    def test_llm_failure_does_not_advance_last_sequence(self):
        _make_slices(3)
        result = run_personality_reflection(
            persona_instance_id="persona-a", thread_id="thread-a", llm=_FakeLLM("not json"),
            min_new_slices=3, min_interval_days=0,
            now="2026-08-10T00:00:00",
        )
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(personality_store.get_last_reflection_run("persona-a", thread_id="thread-a"))
        failed = personality_store.get_reflection_run(result["reflection_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["last_analyzed_sequence"])

    def test_reprocess_can_explicitly_reread_processed_slices(self):
        slices = _make_slices(3)
        content = json.dumps([{
            "trait_id": "openness", "direction": "increase", "strength": 0.2,
            "scope": "general", "behavior_source": "autonomous_choice",
            "evidence_slice_ids": [item.slice_id for item in slices],
            "counter_evidence_slice_ids": [], "confidence": 0.7, "uncertainty": "",
        }], ensure_ascii=False)
        llm = _FakeLLM(content)
        first = run_personality_reflection(
            persona_instance_id="persona-a", thread_id="thread-a", llm=llm,
            min_new_slices=3, min_interval_days=0, now="2026-08-10T00:00:00",
        )
        self.assertEqual(first["status"], "completed")
        second = run_personality_reflection(
            persona_instance_id="persona-a", thread_id="thread-a", llm=llm,
            min_new_slices=3, min_interval_days=0, now="2026-08-10T00:00:00",
            reprocess=True,
        )
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["accepted_count"], 0)
        self.assertEqual(llm.calls, 2)

    def test_interval_blocks_second_reflection(self):
        _make_slices(3)
        llm = _FakeLLM("[]")
        first = run_personality_reflection(
            persona_instance_id="persona-a", thread_id="thread-a", llm=llm,
            min_new_slices=3, min_interval_days=3,
            now="2026-08-10T00:00:00",
        )
        self.assertEqual(first["status"], "completed")
        _make_slices(3, start_index=3)
        second = run_personality_reflection(
            persona_instance_id="persona-a", thread_id="thread-a", llm=llm,
            min_new_slices=1, min_interval_days=7,
            now="2026-08-15T00:00:00",
        )
        self.assertEqual(second["reason"], "reflection_interval_not_reached")

    def test_reflection_watermark_is_scoped_to_thread(self):
        _make_slices(3, thread_id="thread-a")
        llm = _FakeLLM("[]")
        first = run_personality_reflection(
            persona_instance_id="persona-a", thread_id="thread-a", llm=llm,
            min_new_slices=3, min_interval_days=0,
            now="2026-08-10T00:00:00",
        )
        self.assertEqual(first["status"], "completed")
        _make_slices(3, start_index=3, thread_id="thread-b")
        second = run_personality_reflection(
            persona_instance_id="persona-a", thread_id="thread-b", llm=llm,
            min_new_slices=3, min_interval_days=0,
            now="2026-08-10T00:00:00",
        )
        self.assertEqual(second["status"], "completed")
        self.assertEqual(llm.calls, 2)

    def test_evidence_dedupe_is_scoped_to_persona_instance(self):
        first_slices = _make_slices(3, thread_id="thread-a")
        second_slices = _make_slices(3, start_index=3, thread_id="thread-b")
        personality_store.save_personality_seed(
            seed_id="persona-b", persona_instance_id="persona-b", persona_name="Agent B",
            seed={"traits": {"openness": 50}}, source_persona_hash="hash-b",
        )
        def content(slices):
            return json.dumps([{
                "trait_id": "openness", "direction": "increase", "strength": 0.5,
                "scope": "general", "behavior_source": "autonomous_choice",
                "evidence_slice_ids": [item.slice_id for item in slices],
                "counter_evidence_slice_ids": [], "confidence": 0.8, "uncertainty": "",
            }], ensure_ascii=False)
        first = run_personality_reflection(
            persona_instance_id="persona-a", thread_id="thread-a", llm=_FakeLLM(content(first_slices)),
            min_new_slices=3, min_interval_days=0, now="2026-08-10T00:00:00",
        )
        second = run_personality_reflection(
            persona_instance_id="persona-b", thread_id="thread-b", llm=_FakeLLM(content(second_slices)),
            min_new_slices=3, min_interval_days=0, now="2026-08-10T00:00:00",
        )
        self.assertEqual(first["accepted_count"], 1)
        self.assertEqual(second["accepted_count"], 1)
        self.assertEqual(len(personality_store.list_trait_evidence(limit=100)), 2)


if __name__ == "__main__":
    unittest.main()
