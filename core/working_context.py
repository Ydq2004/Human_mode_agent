"""项目自己的短期 checkpoint 上下文整理器。

这里处理的是“当前对话窗口太长”问题，不是长期记忆，也不是人格反思。
达到阈值后，旧消息交给一个专用 LLM 生成工作上下文；原始
``ExperienceSlice`` 不会被改写。发送给主 Agent 的消息始终保持合法的
工具调用顺序，避免把孤立的 ``ToolMessage`` 再送到模型接口。
"""

from __future__ import annotations

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
    """把旧对话总结成短期工作上下文，并保留最近的完整消息组。"""

    def __init__(
        self,
        *,
        summary_llm: Any | None = None,
        trigger_messages: int = 40,
        keep_messages: int = 16,
        max_summary_chars: int = 12000,
    ) -> None:
        super().__init__()
        if trigger_messages < 2:
            raise ValueError("trigger_messages 必须至少为 2")
        if keep_messages < 1 or keep_messages >= trigger_messages:
            raise ValueError("keep_messages 必须大于 0 且小于 trigger_messages")
        if max_summary_chars < 500:
            raise ValueError("max_summary_chars 必须至少为 500")

        self.summary_llm = summary_llm
        self.trigger_messages = trigger_messages
        self.keep_messages = keep_messages
        self.max_summary_chars = max_summary_chars

    def before_model(
        self,
        state: dict[str, Any],
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """在模型调用前总结旧消息，并清理历史中残留的孤立工具结果。"""
        original_messages = list(state.get("messages") or [])
        messages = self._normalize_messages(original_messages)

        # 旧 checkpoint 可能已经保存了孤立 ToolMessage。即使尚未达到总结阈值，
        # 也要先清理它，否则下一次 API 请求仍会被消息协议拒绝。
        if len(messages) < self.trigger_messages:
            if len(messages) != len(original_messages):
                return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]}
            return None

        old_messages, recent_messages = self._split_recent(messages)
        summary, summary_status = self._summarize(old_messages)
        snapshot_message = SystemMessage(
            content=summary,
            additional_kwargs={
                "derived_context": True,
                "source": WORKING_CONTEXT_SOURCE,
                "summary_status": summary_status,
                "preserves_original_experiences": False,
            },
        )

        # RemoveMessage + 新消息会通过 LangGraph 的 messages reducer 更新 checkpoint。
        # 这只改变主 Agent 的短期工作窗口；完整经历仍由 ExperienceSlice 保存。
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                snapshot_message,
                *recent_messages,
            ]
        }

    def _summarize(self, messages: list[AnyMessage]) -> tuple[str, str]:
        """调用专用总结模型；网络失败时使用保守的来源标记快照。"""
        if self.summary_llm is None:
            return self._build_fallback_snapshot(messages), "fallback_no_llm"

        prompt_messages = [
            SystemMessage(content=self._summary_instructions()),
            HumanMessage(content=self._render_transcript(messages)),
        ]
        try:
            response = self.summary_llm.invoke(prompt_messages)
            content = response.content if hasattr(response, "content") else response
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                    for item in content
                )
            summary = str(content or "").strip()
            if summary:
                return self._limit_summary(summary), "llm"
        except Exception:
            # 总结失败不能阻塞主 Agent；回退内容只陈述来源，不添加推断。
            pass

        return self._build_fallback_snapshot(messages), "fallback_error"

    @staticmethod
    def _summary_instructions() -> str:
        return """你是 Human-mode Agent 的短期工作上下文总结器。
你只整理输入中的已发生内容，供下一次主 Agent 调用保持对话连续性。

必须保留：
1. 已确认的用户事实和用户明确要求；
2. Agent 已经说过或做过的行动；
3. 工具调用及工具返回中的客观事实；
4. 事件时间、来源和仍未解决的问题。

必须遵守：
- 不要推断或总结关系进展、人格变化、情绪结论、用户没有明确表达的意图。
- 不确定内容要明确写成“未确定”，不能改成事实。
- 不要编造信息，不要把这份总结写成用户原话。
- 只输出简洁的中文纯文本，使用“已确认事实 / Agent 行动 / 工具事实 /
  未确定内容”四个小标题；没有内容的标题写“无”。
"""

    def _build_fallback_snapshot(self, messages: list[AnyMessage]) -> str:
        """总结模型不可用时的安全回退，不再按消息数硬切断工具组。"""
        lines = [
            "【系统派生的短期工作上下文】",
            "总结模型本轮不可用，以下是按来源整理的原始工作记录。",
            "它不是用户原话，不是长期记忆，也不能替代 ExperienceSlice。",
            "",
        ]
        for index, message in enumerate(messages, start=1):
            content = self._message_text(message)
            if not content:
                continue
            role, source, occurred_at = self._message_origin(message)
            time_text = f"；时间={occurred_at}" if occurred_at else ""
            lines.append(f"{index}. 类型={role}；来源={source}{time_text}；内容={content}")
        return self._limit_summary("\n".join(lines))

    def _render_transcript(self, messages: list[AnyMessage]) -> str:
        lines = ["以下是待整理的旧工作消息，编号仅用于引用，不代表新的事实："]
        for index, message in enumerate(messages, start=1):
            role, source, occurred_at = self._message_origin(message)
            content = self._message_text(message)
            if not content:
                continue
            time_text = f"；时间={occurred_at}" if occurred_at else ""
            derived = "；这是系统派生内容" if self._is_working_snapshot(message) else ""
            lines.append(
                f"{index}. 类型={role}；来源={source}{time_text}{derived}；内容={content}"
            )
        return "\n".join(lines)

    def _limit_summary(self, summary: str) -> str:
        if len(summary) <= self.max_summary_chars:
            return summary
        return (
            summary[: self.max_summary_chars].rstrip()
            + "\n（工作上下文达到安全上限；完整原始经历仍保存在 ExperienceSlice。）"
        )

    @classmethod
    def _normalize_messages(cls, messages: list[AnyMessage]) -> list[AnyMessage]:
        """只保留完整且相邻的工具调用组，去掉孤立或不完整结果。

        OpenAI 风格消息协议要求 ``AIMessage(tool_calls)`` 后面紧接对应的
        ``ToolMessage``。如果旧 checkpoint 中缺一条结果，宁可丢掉这一组的
        工作上下文，也不把半组消息送给模型；完整经历仍保存在 ExperienceSlice。
        """
        normalized: list[AnyMessage] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if isinstance(message, AIMessage):
                calls = [
                    str(call.get("id") or "").strip()
                    for call in message.tool_calls or []
                ]
                call_ids = {call_id for call_id in calls if call_id}
                if not call_ids:
                    normalized.append(message)
                    index += 1
                    continue

                group = [message]
                result_ids: set[str] = set()
                cursor = index + 1
                while cursor < len(messages) and isinstance(messages[cursor], ToolMessage):
                    result = messages[cursor]
                    result_id = str(result.tool_call_id or "").strip()
                    if result_id in call_ids and result_id not in result_ids:
                        group.append(result)
                        result_ids.add(result_id)
                    cursor += 1

                if result_ids == call_ids:
                    normalized.extend(group)
                # 缺结果或重复/未知结果时丢弃整个工具组，避免产生非法历史。
                index = cursor
                continue

            if isinstance(message, ToolMessage):
                # 没有紧邻前置 AI tool_calls 的 ToolMessage 是孤立结果。
                index += 1
                continue

            normalized.append(message)
            index += 1
        return normalized

    def _split_recent(self, messages: list[AnyMessage]) -> tuple[list[AnyMessage], list[AnyMessage]]:
        """找到最近窗口；若起点落在工具结果中间，则向前扩展到 AI 调用。"""
        start = max(0, len(messages) - self.keep_messages)
        if start < len(messages) and isinstance(messages[start], ToolMessage):
            call_id = str(messages[start].tool_call_id or "").strip()
            for index in range(start - 1, -1, -1):
                candidate = messages[index]
                if isinstance(candidate, AIMessage) and any(
                    str(call.get("id") or "").strip() == call_id
                    for call in candidate.tool_calls or []
                ):
                    start = index
                    break
        return messages[:start], messages[start:]

    @staticmethod
    def _is_working_snapshot(message: AnyMessage) -> bool:
        return (
            isinstance(message, SystemMessage)
            and message.additional_kwargs.get("source") == WORKING_CONTEXT_SOURCE
        )

    @staticmethod
    def _message_text(message: AnyMessage) -> str:
        content = message.content
        if isinstance(content, str):
            text = " ".join(content.split())
        elif isinstance(content, list):
            text = " ".join(str(item) for item in content)
        else:
            text = str(content)

        if isinstance(message, AIMessage) and message.tool_calls:
            calls = "; ".join(
                f"{call.get('name', 'tool')}({call.get('id', '')})"
                for call in message.tool_calls
            )
            text = f"{text}；工具调用={calls}" if text else f"工具调用={calls}"
        return text[:2000]

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
