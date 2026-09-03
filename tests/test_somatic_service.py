import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.somatic_service import SomaticService
from memory import somatic_store


class _Response:
    def __init__(self, content):
        self.content = content


class _LLM:
    def bind(self, **kwargs):
        return self

    def invoke(self, prompt):
        return _Response(json.dumps({
            "arousal_change": "increase",
            "arousal_magnitude": "slight",
            "sensitivity_change": "unchanged",
            "sensitivity_magnitude": "slight",
            "visible_reaction": "肩膀微微一动",
            "interoception_description": "身体感到一阵温热",
        }, ensure_ascii=False))


class SomaticServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            somatic_store,
            "SQLITE_DB_PATH",
            str(Path(self.temp_dir.name) / "somatic.db"),
        )
        self.db_patch.start()
        self.service = SomaticService(
            persona_instance_id="persona",
            thread_id="thread",
            llm=_LLM(),
        )

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def _request(self, confirmed=True):
        return {
            "event_confirmed": confirmed,
            "contact_object": "user",
            "contact_area": "hand",
            "intensity": "gentle",
            "duration": "brief",
            "uncertainty": "low",
        }

    def test_occurred_event_changes_state_and_returns_feedback(self):
        outcome = self.service.process(
            self._request(),
            source_event_id="evt-1",
            operation_id="op-1",
            source_kind="perceived_contact",
        )
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.state_after.values["arousal"], 0.55)
        self.assertTrue(outcome.interoception_description)

    def test_rejected_event_does_not_change_state(self):
        outcome = self.service.process(
            self._request(False),
            source_event_id="evt-1",
            operation_id="op-1",
            source_kind="perceived_contact",
        )
        self.assertEqual(outcome.status, "rejected")
        self.assertEqual(outcome.state_after.to_dict(), outcome.state_before.to_dict())

    def test_duplicate_operation_does_not_apply_twice(self):
        first = self.service.process(
            self._request(), source_event_id="evt-1", operation_id="op-1", source_kind="perceived_contact"
        )
        second = self.service.process(
            self._request(), source_event_id="evt-1", operation_id="op-1", source_kind="perceived_contact"
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.revision, 1)

    def test_agent_action_can_be_disabled_by_persona_config(self):
        service = SomaticService(
            persona_instance_id="persona",
            thread_id="other",
            llm=_LLM(),
            allow_agent_initiated_actions=False,
        )
        outcome = service.process(
            {
                **self._request(),
                "action_description": "抱住对方",
                "target": "user",
            },
            source_event_id="evt-2",
            operation_id="op-2",
            source_kind="agent_action",
        )
        self.assertEqual(outcome.status, "rejected")


if __name__ == "__main__":
    unittest.main()
