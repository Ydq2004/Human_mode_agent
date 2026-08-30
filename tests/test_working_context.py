import sys
import unittest
from pathlib import Path

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

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

    def test_threshold_uses_project_llm_summary_as_system_context(self):
        class FakeSummaryLLM:
            def __init__(self):
                self.calls = []

            def invoke(self, messages):
                self.calls.append(messages)
                return AIMessage(content="已确认事实：昨天发生过一次问候。")

        summary_llm = FakeSummaryLLM()
        middleware = WorkingContextMiddleware(
            summary_llm=summary_llm,
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
        self.assertEqual(snapshot.additional_kwargs["summary_status"], "llm")
        self.assertEqual(snapshot.content, "已确认事实：昨天发生过一次问候。")
        self.assertEqual(len(summary_llm.calls), 1)
        self.assertIn("昨天的原话", summary_llm.calls[0][1].content)
        self.assertIn("2026-08-23T20:00:00+08:00", summary_llm.calls[0][1].content)
        self.assertEqual(updated[-1].content, "最近回答")
        # before_model 返回的是增量更新，不能修改传入的原始状态。
        self.assertEqual(state["messages"][0].content, "昨天的原话")

    def test_summary_failure_uses_conservative_fallback(self):
        class FailingSummaryLLM:
            def invoke(self, messages):
                raise RuntimeError("network down")

        middleware = WorkingContextMiddleware(
            summary_llm=FailingSummaryLLM(),
            trigger_messages=3,
            keep_messages=1,
        )
        state = {
            "messages": [
                HumanMessage(content="明确事实"),
                AIMessage(content="旧回复"),
                HumanMessage(content="新问题"),
            ]
        }

        update = middleware.before_model(state, None)
        snapshot = update["messages"][1]
        self.assertEqual(snapshot.additional_kwargs["summary_status"], "fallback_error")
        self.assertIn("明确事实", snapshot.content)
        self.assertIn("系统派生", snapshot.content)

    def test_orphan_tool_messages_are_removed_before_model(self):
        middleware = WorkingContextMiddleware(trigger_messages=10, keep_messages=2)
        state = {
            "messages": [
                HumanMessage(content="用户问题"),
                ToolMessage(content="孤立结果", tool_call_id="missing"),
                AIMessage(content="正常回复"),
            ]
        }

        update = middleware.before_model(state, None)
        self.assertIsNotNone(update)
        cleaned = update["messages"]
        self.assertIsInstance(cleaned[0], RemoveMessage)
        self.assertEqual([item.content for item in cleaned[1:]], ["用户问题", "正常回复"])

    def test_tool_call_group_is_kept_together_when_recent_window_starts_at_result(self):
        middleware = WorkingContextMiddleware(
            summary_llm=None,
            trigger_messages=5,
            keep_messages=2,
        )
        tool_call = AIMessage(
            content="",
            tool_calls=[{"name": "recall_memory", "args": {}, "id": "call-1"}],
        )
        state = {
            "messages": [
                HumanMessage(content="旧问题"),
                HumanMessage(content="另一个旧问题"),
                tool_call,
                ToolMessage(content="查询结果", tool_call_id="call-1"),
                HumanMessage(content="当前问题"),
            ]
        }

        update = middleware.before_model(state, None)
        recent = update["messages"][2:]
        self.assertIs(recent[0], tool_call)
        self.assertIsInstance(recent[1], ToolMessage)
        self.assertEqual(recent[1].tool_call_id, "call-1")

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
        self.assertIn("系统派生的短期工作上下文", snapshot.content)
        self.assertIn("旧内容", snapshot.content)
        self.assertNotIn("source=human", snapshot.content)


if __name__ == "__main__":
    unittest.main()
