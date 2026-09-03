import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

import core.agent_factory as agent_factory
from core.appraisal_worker import AppraisalWorker
from core.somatic_service import SomaticService
from core.perception import create_perception_event
from main import _extract_tool_interactions, process_perception_event
from memory import somatic_store


class _Response:
    def __init__(self, content): self.content = content


class _SomaticLLM:
    def bind(self, **kwargs): return self
    def invoke(self, prompt):
        return _Response(json.dumps({
            "arousal_change": "increase",
            "arousal_magnitude": "slight",
            "sensitivity_change": "increase",
            "sensitivity_magnitude": "slight",
            "visible_reaction": "手指轻轻收紧",
            "interoception_description": "掌心传来温热触感",
        }, ensure_ascii=False))


class _Agent:
    def invoke(self, payload, config):
        assert "掌心传来温热触感" in agent_factory._RUNTIME_INJECTION["text"]
        return {"messages": [AIMessage(content="我感受到了。")]} 


class _Appraisal:
    def to_dict(self): return {"status": "fake"}


class MainSomaticIntegrationTests(unittest.TestCase):
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

    def test_perceived_contact_reaches_agent_in_same_turn_and_is_recorded(self):
        service = SomaticService(
            persona_instance_id="persona",
            thread_id="thread",
            llm=_SomaticLLM(),
        )
        worker = AppraisalWorker(
            object(),
            appraise_fn=lambda **kwargs: _Appraisal(),
            effects_fn=lambda **kwargs: {"mood": {"mood_impact": 0}},
        )
        context = {
            "injection_text": "base\n",
            "perception_understanding": {
                "understanding_status": "normal",
                "module_requests": [{
                    "module": "somatic",
                    "event_candidate": {
                        "event_confirmed": True,
                        "contact_object": "user",
                        "target": "agent",
                        "contact_area": "hand",
                        "intensity": "gentle",
                        "duration": "brief",
                        "uncertainty": "low",
                    },
                }],
            },
            "activated_memory_refs": [],
            "memory_activation_state": {},
            "timings": {},
        }
        try:
            with (
                patch("main.begin_perception_event", return_value={"mood": 50, "energy": 100}),
                patch("main.build_agent_context", return_value=context),
                patch("main.save_experience_slice"),
                patch("main.commit_store.record_appraisal_pending"),
            ):
                result = process_perception_event(
                    create_perception_event("user", "text", "（轻轻碰了碰你的手）"),
                    agent=_Agent(),
                    agent_config={"configurable": {"thread_id": "thread"}},
                    understanding_llm=object(),
                    retry_understanding_llm=object(),
                    appraisal_worker=worker,
                    thread_id="thread",
                    persona={
                        "agent_name": "persona",
                        "emotion_profile": {"mood_reactivity": 1.0},
                    },
                    recent_context=[],
                    event_sequence=1,
                    somatic_service=service,
                )
            experience = result["experience_slice"]
            self.assertEqual(result["visible_reply"], "我感受到了。")
            self.assertIn("somatic_response", [x.action_type for x in experience.response_or_actions])
            self.assertIn("interoception", [x.modality for x in experience.observations])
            self.assertEqual(service.current_state().revision, 1)
        finally:
            worker.shutdown(wait=True)

    def test_somatic_tool_result_becomes_action_and_interoception(self):
        payload = {
            "status": "success",
            "outcome": {
                "status": "completed",
                "operation_id": "op-1",
                "action_description": "轻轻抱住对方",
                "visible_reaction": "手臂微微收紧",
                "interoception_description": "感到贴近的温度",
                "decision": {"status": "occurred"},
                "state_before": {"arousal": 0.5, "sensitivity": 0.5},
                "state_after": {"arousal": 0.55, "sensitivity": 0.55},
            },
        }
        messages = [
            AIMessage(content="", tool_calls=[{
                "name": "somatic_action", "args": {}, "id": "call-1", "type": "tool_call"
            }]),
            ToolMessage(
                content=json.dumps(payload, ensure_ascii=False),
                name="somatic_action",
                tool_call_id="call-1",
            ),
        ]
        actions, observations = _extract_tool_interactions(messages)
        action_types = [item.action_type for item in actions]
        self.assertIn("somatic_action", action_types)
        self.assertIn("somatic_response", action_types)
        self.assertIn("interoception", [item.modality for item in observations])


if __name__ == "__main__":
    unittest.main()
