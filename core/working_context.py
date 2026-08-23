"""项目自己的短期 checkpoint 上下文整理器。

这里处理的是“当前对话窗口太长”问题，不是长期记忆，也不是人格反思。
整理过程不调用 LLM，避免每次达到阈值时额外增加一次网络请求；旧消息会被
替换为一条明确标记的 SystemMessage，最近消息继续保留原文。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime


WORKING_CONTEXT_SOURCE = "project_working_context"


class WorkingContextMiddleware(AgentMiddleware):
    """达到阈值后裁剪 checkpoint，并保留可追踪的短期工作快照。"""

    def __init__(
        self,
        *,
        trigger_messages: int = 40,
        keep_messages: int = 16,
        max_snapshot_chars: int = 8000,
    ) -> None:
        super().__init__()
        if trigger_messages < 2:
            raise ValueError("trigger_messages 必须至少为 2")
        if keep_messages < 1 or keep_messages >= trigger_messages:
            raise ValueError("keep_messages 必须大于 0 且小于 trigger_messages")
        if max_snapshot_chars < 500:
            raise ValueError("max_snapshot_chars 必须至少为 500")

        self.trigger_messages = trigger_messages
        self.keep_messages = keep_messages
        self.max_snapshot_chars = max_snapshot_chars

    def before_model(
        self,
        state: dict[str, Any],
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """在模型调用前按消息数量裁剪活动 checkpoint。"""
        messages = list(state.get("messages") or [])
        if len(messages) < self.trigger_messages:
            return None

        old_messages = messages[:-self.keep_messages]
        recent_messages = messages[-self.keep_messages:]
        snapshot = self._build_snapshot(old_messages)
        snapshot_message = SystemMessage(
            content=snapshot,
            additional_kwargs={
                "derived_context": True,
                "source": WORKING_CONTEXT_SOURCE,
                "preserves_original_experiences": False,
            },
        )

        # RemoveMessage + 新消息会通过 LangGraph 的 messages reducer 更新
        # checkpoint。原始 ExperienceSlice 不受这次裁剪影响。
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                snapshot_message,
                *recent_messages,
            ]
        }

    def _build_snapshot(self, messages: list[AnyMessage]) -> str:
        lines = [
            "【系统派生的短期工作上下文】",
            "以下内容由框架从旧消息整理，仅用于保持当前对话连续性。",
            "它不是用户原话，不是长期记忆，也不能替代 ExperienceSlice。",
            "如果与当前明确事件冲突，以当前事件和结构化长期认知为准。",
            "",
        ]

        for index, message in enumerate(messages, start=1):
            if self._is_working_snapshot(message):
                text = str(message.content or "").strip()
                if text:
                    lines.append(f"已有工作上下文 {index}：{text}")
                continue

            role, source, occurred_at = self._message_origin(message)
            content = self._message_text(message)
            if not content:
                continue

            time_text = f"；时间={occurred_at}" if occurred_at else ""
            lines.append(
                f"{index}. 类型={role}；来源={source}{time_text}；内容={content}"
            )

        rendered = "\n".join(lines)
        if len(rendered) <= self.max_snapshot_chars:
            return rendered

        return rendered[: self.max_snapshot_chars].rstrip() + "\n（旧工作上下文已按长度截断；原始经历仍保存在 ExperienceSlice。）"

    @staticmethod
    def _is_working_snapshot(message: AnyMessage) -> bool:
        return (
            isinstance(message, SystemMessage)
            and message.additional_kwargs.get("source")
            == WORKING_CONTEXT_SOURCE
        )

    @staticmethod
    def _message_text(message: AnyMessage) -> str:
        content = message.content
        if isinstance(content, str):
            return " ".join(content.split())[:1000]
        return str(content)[:1000]

    @staticmethod
    def _message_origin(message: AnyMessage) -> tuple[str, str, str]:
        if isinstance(message, HumanMessage):
            role = "human"
        elif isinstance(message, AIMessage):
            role = "agent"
        elif isinstance(message, ToolMessage):
            role = "tool"
        elif isinstance(message, SystemMessage):
            role = "system"
        else:
            role = type(message).__name__

        event_data = message.additional_kwargs.get("perception_event")
        if isinstance(event_data, dict):
            return (
                role,
                str(event_data.get("source") or "perception_event"),
                str(event_data.get("occurred_at") or ""),
            )

        return role, role, ""
