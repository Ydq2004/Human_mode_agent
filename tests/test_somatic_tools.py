import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.somatic_service import SomaticService
from memory import somatic_store
from tools.somatic_tools import somatic_action, somatic_tool_context


class _LLM:
    def bind(self, **kwargs): return self
    def invoke(self, prompt):
        class Response:
            content = json.dumps({
                "arousal_change": "increase", "arousal_magnitude": "slight",
                "sensitivity_change": "unchanged", "sensitivity_magnitude": "slight",
                "visible_reaction": "手臂收紧", "interoception_description": "感到温暖",
            }, ensure_ascii=False)
        return Response()


class SomaticToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            somatic_store,
            "SQLITE_DB_PATH",
            str(Path(self.temp_dir.name) / "somatic.db"),
        )
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_tool_requires_runtime_context(self):
        result = somatic_action.invoke({
            "name": "somatic_action",
            "type": "tool_call",
            "id": "call-1",
            "args": {
                "action_description": "拥抱",
                "target": "user",
                "contact_area": "arms",
                "intensity": "gentle",
                "duration": "brief",
            },
        })
        payload = json.loads(result.content)
        self.assertEqual(payload["status"], "error")

    def test_tool_returns_feedback(self):
        service = SomaticService(persona_instance_id="p", thread_id="t", llm=_LLM())
        with somatic_tool_context(service, source_event_id="evt-1"):
            result = somatic_action.invoke({
                "name": "somatic_action",
                "type": "tool_call",
                "id": "call-1",
                "args": {
                    "action_description": "拥抱",
                    "target": "user",
                    "contact_area": "arms",
                    "intensity": "gentle",
                    "duration": "brief",
                },
            })
        payload = json.loads(result.content)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["outcome"]["source_kind"], "agent_action")
        self.assertNotIn("state_before", payload["outcome"])
        self.assertNotIn("state_after", payload["outcome"])
        self.assertNotIn("revision", payload["outcome"])
        self.assertEqual(
            result.artifact["outcome"]["state_after"],
            {"arousal": 0.55, "sensitivity": 0.5},
        )


if __name__ == "__main__":
    unittest.main()
