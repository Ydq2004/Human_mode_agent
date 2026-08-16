import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.experience import create_agent_action, create_experience_slice
from core.perception import create_perception_event
from main import _print_appraisal_job, _print_frontend_debug


class FakeAppraisal:
    def to_dict(self):
        return {
            "experience_review": {
                "experience_summary": "测试评价",
            },
        }


class MainDebugOutputTests(unittest.TestCase):

    def _create_experience(self):
        event = create_perception_event(
            "user",
            "text",
            "测试消息",
            occurred_at="2026-08-11T12:00:00",
        )
        return create_experience_slice(
            perception_event=event,
            perception_understanding={
                "situated_understanding": "测试理解",
                "understanding_status": "normal",
                "memory_activation_cues": [],
                "uncertainties": [],
            },
            activated_memory_refs=[],
            response_or_actions=[
                create_agent_action("visible_response", "测试回复"),
            ],
            observations=[],
            state_snapshot={"owner": "agent", "mood": 50},
        )

    def test_frontend_debug_is_split_into_readable_sections(self):
        experience = self._create_experience()
        output = StringIO()

        result = {
            "context_result": {
                "perception_understanding": {
                    "situated_understanding": "测试理解",
                    "understanding_status": "normal",
                    "memory_activation_cues": [],
                    "uncertainties": [],
                },
                "activated_memory_refs": [],
                "memory_activation_state": {
                    "status": "normal",
                    "exhaustive": False,
                    "absence_means_not_exists": False,
                },
                "retrieval_debug": {"queries": ["测试"]},
            },
            "experience_slice": experience,
            "appraisal_job": {
                "status": "pending",
                "job_id": "appraisal_test",
                "event_id": experience.perception_event.event_id,
                "experience_slice_id": experience.slice_id,
                "submitted_at": "2026-08-11T12:00:01",
            },
            "timings": {
                "understanding_seconds": 0.1234,
                "retrieval_seconds": 0.02,
                "main_agent_seconds": 1.5,
            },
        }

        with redirect_stdout(output):
            _print_frontend_debug(result)

        text = output.getvalue()
        self.assertIn("调试 | 前台处理完成", text)
        self.assertIn("[耗时]", text)
        self.assertIn("感知理解：0.123 秒", text)
        self.assertIn("[本轮自然浮现的认知（0 条）]", text)
        self.assertIn("[ExperienceSlice（完整结构）]", text)
        self.assertIn('"situated_understanding": "测试理解"', text)
        self.assertIn('"status": "pending"', text)

    def test_completed_appraisal_has_its_own_debug_block(self):
        output = StringIO()
        job = {
            "status": "completed",
            "job_id": "appraisal_test",
            "event_id": "evt_test",
            "experience_slice_id": "exp_test",
            "submitted_at": "2026-08-11T12:00:01",
            "completed_at": "2026-08-11T12:00:03",
            "appraisal": FakeAppraisal(),
            "effects": {"mood": {"mood_impact": 1}},
            "timings": {
                "appraisal_seconds": 1.2,
                "rules_seconds": 0.01,
            },
            "error": None,
        }

        with redirect_stdout(output):
            _print_appraisal_job(job)

        text = output.getvalue()
        self.assertIn("调试 | 后台评价完成", text)
        self.assertIn("经验评价 LLM：1.200 秒", text)
        self.assertIn("[ExperienceAppraisal]", text)
        self.assertIn("[AppraisalEffects（提交候选）]", text)
        self.assertIn('"mood_impact": 1', text)


if __name__ == "__main__":
    unittest.main()
