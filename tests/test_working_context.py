import sys
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.working_context import WORKING_CONTEXT_SOURCE, WorkingContextMiddleware


class WorkingContextMiddlewareTests(unittest.TestCase):
    def test_below_threshold_does_not_change_state(self):
        middleware = WorkingContextMiddleware(
            trigger_messages=4,
            keep_messages=2,
        )
        state = {"messages": [HumanMessage(content="你好")]}
        self.assertIsNone(middleware.before_model(state, None))

    def test_threshold_replaces_old_messages_with_explicit_system_snapshot(self):
        middleware = WorkingContextMiddleware(
            trigger_messages=4,
            keep_messages=2,
        )
        old_event = HumanMessage(
            content="昨天的原话",
            additional_kwargs={
                "perception_event": {
                    "source": "user",
                    "occurred_at": "2026-08-23T20:00:00+08:00",
                }
            },
        )
        state = {
            "messages": [
                old_event,
                AIMessage(content="旧回复"),
                HumanMessage(content="最近问题"),
                AIMessage(content="最近回答"),
            ]
        }

        update = middleware.before_model(state, None)
        self.assertIsNotNone(update)
        updated = update["messages"]
        self.assertIsInstance(updated[0], RemoveMessage)
        self.assertEqual(len(updated), 4)
        snapshot = updated[1]
        self.assertEqual(snapshot.additional_kwargs["source"], WORKING_CONTEXT_SOURCE)
        self.assertTrue(snapshot.additional_kwargs["derived_context"])
        self.assertIn("昨天的原话", snapshot.content)
        self.assertIn("2026-08-23T20:00:00+08:00", snapshot.content)
        self.assertEqual(updated[-1].content, "最近回答")
        # before_model 返回的是增量更新，不能修改传入的原始状态。
        self.assertEqual(state["messages"][0].content, "昨天的原话")

    def test_existing_snapshot_is_marked_as_derived_not_user_fact(self):
        middleware = WorkingContextMiddleware(
            trigger_messages=3,
            keep_messages=1,
        )
        state = {
            "messages": [
                SystemMessage(
                    content="【系统派生的短期工作上下文】旧内容",
                    additional_kwargs={"source": WORKING_CONTEXT_SOURCE},
                ),
                HumanMessage(content="新问题"),
                AIMessage(content="新回答"),
            ]
        }
        update = middleware.before_model(state, None)
        snapshot = update["messages"][1]
        self.assertIn("已有工作上下文", snapshot.content)
        self.assertNotIn("source=human", snapshot.content)


if __name__ == "__main__":
    unittest.main()
