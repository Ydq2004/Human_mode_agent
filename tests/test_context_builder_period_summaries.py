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


if __name__ == "__main__":
    unittest.main()
