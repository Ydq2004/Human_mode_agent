import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.runtime import ApplicationRuntime, RuntimeClosedError  # noqa: E402
from core.perception import create_perception_event  # noqa: E402


class ApplicationRuntimeTests(unittest.TestCase):
    def _runtime_without_services(self) -> ApplicationRuntime:
        runtime = object.__new__(ApplicationRuntime)
        runtime.thread_id = "long-lived"
        runtime._file_confirm = lambda _request: False
        runtime._event_lock = threading.RLock()
        runtime._closed = False
        runtime._recent_context = []
        runtime.event_sequence = 0
        runtime.agent = object()
        runtime.agent_config = {"configurable": {"thread_id": "long-lived"}}
        runtime.understanding_llm = object()
        runtime.retry_understanding_llm = object()
        runtime.appraisal_worker = object()
        runtime.persona = {
            "agent_name": "测试角色",
            "emotion_profile": {"mood_reactivity": 1.0},
        }
        runtime.capability_snapshot = {}
        runtime.somatic_service = None
        runtime.vision_service = None
        runtime.background_worker = None
        runtime.background_event_bus = None
        runtime.advanced_llm = None
        runtime.persona_instance_id = "persona_test"
        runtime.image_generation_service = None
        return runtime

    def test_submit_text_serializes_event_and_updates_sequence(self):
        runtime = self._runtime_without_services()
        event = create_perception_event("user", "text", "你好")
        experience = SimpleNamespace()
        result = {"experience_slice": experience, "visible_reply": "收到"}

        with (
            patch("app.runtime.process_perception_event", return_value=result) as process,
            patch("app.runtime._working_context_from_experience", return_value="最近一轮"),
        ):
            returned = runtime.submit_text("你好")

        self.assertIs(returned, result)
        self.assertEqual(runtime.event_sequence, 1)
        self.assertEqual(runtime._recent_context, ["最近一轮"])
        self.assertEqual(process.call_args.args[0].source, "user")
        self.assertEqual(process.call_args.args[0].modality, "text")
        self.assertEqual(process.call_args.kwargs["thread_id"], "long-lived")
        self.assertEqual(process.call_args.kwargs["event_sequence"], 1)

    def test_closed_runtime_rejects_new_event(self):
        runtime = self._runtime_without_services()
        runtime._closed = True

        with self.assertRaises(RuntimeClosedError):
            runtime.submit_text("不能提交")

    def test_thread_id_is_validated_as_settings_value(self):
        with self.assertRaises(ValueError):
            ApplicationRuntime._validate_thread_id("")
        with self.assertRaises(ValueError):
            ApplicationRuntime._validate_thread_id("a\nthread")
        self.assertEqual(
            ApplicationRuntime._validate_thread_id("project_long_lived"),
            "project_long_lived",
        )


if __name__ == "__main__":
    unittest.main()
