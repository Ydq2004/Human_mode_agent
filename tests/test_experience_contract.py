"""第 0 步：ExperienceSlice 上下文契约测试。"""

import sys
from pathlib import Path
import unittest

# 允许从项目根目录或直接以 tests/test_*.py 的路径运行。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.experience import create_agent_action, create_experience_slice
from core.perception import create_perception_event


class ExperienceSliceContractTests(unittest.TestCase):
    def _create_slice(self, preceding_context=None):
        event = create_perception_event(
            source="user",
            modality="text",
            content="你好",
        )
        action = create_agent_action(
            action_type="visible_response",
            content="你好。",
        )
        return create_experience_slice(
            perception_event=event,
            perception_understanding={
                "situated_understanding": "普通问候",
                "understanding_status": "normal",
                "memory_activation_cues": [],
                "uncertainties": [],
            },
            activated_memory_refs=[],
            response_or_actions=[action],
            observations=[],
            state_snapshot={"owner": "agent", "mood": 50},
            capability_snapshot={
                "memory_access": {
                    "automatic_activation": {
                        "available": True,
                        "exhaustive": False,
                    },
                },
            },
            memory_activation_state={
                "status": "normal",
                "exhaustive": False,
                "absence_means_not_exists": False,
            },
            preceding_context=preceding_context,
        )

    def test_preceding_context_is_serialized_separately(self):
        context = {
            "recent_relevant_events": [{"summary": "上一轮问候"}],
        }
        experience = self._create_slice(context)

        self.assertEqual(
            experience.to_dict()["preceding_context"],
            context,
        )
        self.assertNotIn("persona_context", experience.to_dict())

    def test_preceding_context_is_frozen_from_source_mutation(self):
        context = {
            "recent_relevant_events": [{"summary": "上一轮"}],
        }
        experience = self._create_slice(context)
        context["recent_relevant_events"][0]["summary"] = "被外部修改"

        self.assertEqual(
            experience.preceding_context[
                "recent_relevant_events"
            ][0]["summary"],
            "上一轮",
        )
        with self.assertRaises(TypeError):
            experience.preceding_context["recent_relevant_events"] = []

    def test_factory_keeps_backward_compatible_empty_context(self):
        experience = self._create_slice()

        self.assertEqual(experience.preceding_context, {})
        self.assertEqual(experience.to_dict()["preceding_context"], {})

    def test_capability_and_activation_state_are_serialized(self):
        payload = self._create_slice().to_dict()

        self.assertTrue(
            payload["capability_snapshot"]["memory_access"]
            ["automatic_activation"]["available"]
        )
        self.assertFalse(payload["memory_activation_state"]["exhaustive"])
        self.assertFalse(
            payload["memory_activation_state"]["absence_means_not_exists"]
        )

    def test_capability_and_activation_state_are_frozen(self):
        capability = {
            "memory_access": {
                "automatic_activation": {"available": True},
            },
        }
        activation_state = {"status": "normal", "exhaustive": False}
        experience = create_experience_slice(
            perception_event=create_perception_event("user", "text", "你好"),
            perception_understanding={},
            activated_memory_refs=[],
            response_or_actions=[],
            observations=[],
            state_snapshot={"owner": "agent", "mood": 50},
            capability_snapshot=capability,
            memory_activation_state=activation_state,
        )

        capability["memory_access"]["automatic_activation"]["available"] = False
        activation_state["status"] = "failed"

        self.assertTrue(
            experience.capability_snapshot["memory_access"]
            ["automatic_activation"]["available"]
        )
        self.assertEqual(experience.memory_activation_state["status"], "normal")
        with self.assertRaises(TypeError):
            experience.capability_snapshot["memory_access"] = {}


if __name__ == "__main__":
    unittest.main()
