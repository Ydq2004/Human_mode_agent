import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.context_builder import build_agent_context
from core.perception import PerceptionFrame, create_perception_event


class ContextBuilderPeriodSummaryTests(unittest.TestCase):
    def test_latest_daily_weekly_monthly_summaries_are_background_only(self):
        summaries = {
            "daily": [
                SimpleNamespace(
                    start_at="2026-08-29T00:00:00",
                    end_at="2026-08-29T23:59:59",
                    source_slice_ids=("daily-1",),
                    summary_text="日摘要：用户提出了一个项目问题。",
                )
            ],
            "weekly": [
                SimpleNamespace(
                    start_at="2026-08-24T00:00:00",
                    end_at="2026-08-30T23:59:59",
                    source_slice_ids=("weekly-1", "weekly-2"),
                    summary_text="周摘要：项目完成了阶段性测试。",
                )
            ],
            "monthly": [
                SimpleNamespace(
                    start_at="2026-08-01T00:00:00",
                    end_at="2026-08-31T23:59:59",
                    source_slice_ids=("monthly-1",),
                    summary_text="月摘要：系统持续积累了可追踪经历。",
                )
            ],
        }

        def fake_list_summaries(*, summary_type, limit):
            self.assertEqual(limit, 100)
            return summaries[summary_type]

        frame = PerceptionFrame(
            perception_event=create_perception_event("user", "text", "当前问题"),
            state_snapshot={"owner": "agent", "mood": 50, "energy": 100},
        )
        with patch("core.context_builder.list_summaries", side_effect=fake_list_summaries):
            result = build_agent_context(frame)

        text = result["injection_text"]
        self.assertIn("最近的日摘要", text)
        self.assertIn("日摘要：用户提出了一个项目问题", text)
        self.assertIn("最近的周摘要", text)
        self.assertIn("最近的月摘要", text)
        self.assertIn("需要具体过程时，请调用只读 recall_memory", text)

    def test_latest_personality_snapshot_is_rendered_for_agent_only(self):
        frame = PerceptionFrame(
            perception_event=create_perception_event("user", "text", "当前问题"),
            state_snapshot={"owner": "agent", "mood": 50, "energy": 100},
        )
        with patch("core.context_builder.list_summaries", return_value=[]), patch(
            "core.context_builder.get_latest_personality_snapshot",
            return_value={"trait_values": {"openness": 70.0}},
        ):
            result = build_agent_context(frame, persona_instance_id="persona-a")

        self.assertIn("当前人格倾向（由长期经历派生）", result["injection_text"])
        self.assertIn("开放性", result["injection_text"])

    def test_missing_personality_snapshot_falls_back_silently(self):
        frame = PerceptionFrame(
            perception_event=create_perception_event("user", "text", "当前问题"),
            state_snapshot={"owner": "agent", "mood": 50, "energy": 100},
        )
        with patch("core.context_builder.list_summaries", return_value=[]), patch(
            "core.context_builder.get_latest_personality_snapshot", return_value=None,
        ):
            result = build_agent_context(frame, persona_instance_id="persona-a")

        self.assertNotIn("当前人格倾向（由长期经历派生）", result["injection_text"])

    def test_internal_somatic_values_are_not_rendered_for_agent(self):
        frame = PerceptionFrame(
            perception_event=create_perception_event("user", "text", "当前问题"),
            state_snapshot={
                "owner": "agent",
                "mood": 50,
                "energy": 100,
                "somatic": {
                    "values": {"arousal": 0.8, "sensitivity": 0.95},
                    "revision": 13,
                },
            },
        )
        with patch("core.context_builder.list_summaries", return_value=[]), patch(
            "core.context_builder.get_latest_personality_snapshot",
            return_value=None,
        ):
            result = build_agent_context(frame)

        text = result["injection_text"]
        self.assertNotIn("当前身体状态", text)
        self.assertNotIn("arousal", text)
        self.assertNotIn("sensitivity", text)
        self.assertNotIn("0.8", text)
        self.assertNotIn("0.95", text)


if __name__ == "__main__":
    unittest.main()
