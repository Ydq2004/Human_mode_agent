"""主 Agent 使用的叙事身体行动工具。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Iterator

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool

from config import SOMATIC_MAX_CALLS_PER_TURN
from core.somatic_service import SomaticService


@dataclass
class _ToolRuntime:
    service: SomaticService
    source_event_id: str
    count: int = 0
    consent_status: str = "unknown"


_RUNTIME: ContextVar[_ToolRuntime | None] = ContextVar("somatic_tool_runtime", default=None)


@contextmanager
def somatic_tool_context(
    service: SomaticService,
    *,
    source_event_id: str,
    consent_status: str = "unknown",
) -> Iterator[None]:
    """把当前实例身体服务绑定到一次 Agent 调用，结束后立即清理。"""
    token = _RUNTIME.set(_ToolRuntime(service=service, source_event_id=source_event_id, consent_status=str(consent_status or "unknown")))
    try:
        yield
    finally:
        _RUNTIME.reset(token)


def _message(
    tool_call_id: str,
    payload: dict,
    *,
    artifact: dict | None = None,
) -> ToolMessage:
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        name="somatic_action",
        tool_call_id=tool_call_id,
        artifact=artifact if artifact is not None else payload,
    )


@tool
def somatic_action(
    action_description: str,
    target: str,
    contact_area: str,
    intensity: str,
    duration: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> ToolMessage:
    """执行一次新的具身动作，并返回身体模块的外在反应与内感受。

    仅在确实要执行主动动作时调用。参数描述动作、目标、接触部位、强度和持续时间；
    工具返回的 status 是这次动作是否在当前执行通道中成立的依据。
    """
    runtime = _RUNTIME.get()
    if runtime is None:
        return _message(tool_call_id, {
            "operation": "somatic_action",
            "status": "error",
            "reason": "当前没有可用的身体实例上下文",
        })
    runtime.count += 1
    if runtime.count > SOMATIC_MAX_CALLS_PER_TURN:
        return _message(tool_call_id, {
            "operation": "somatic_action",
            "status": "call_limit_reached",
            "count": runtime.count,
            "limit": SOMATIC_MAX_CALLS_PER_TURN,
        })

    request = {
        "event_confirmed": True,
        "contact_object": "agent",
        "contact_area": contact_area,
        "intensity": intensity,
        "duration": duration,
        "uncertainty": "low",
        "action_description": str(action_description or "").strip(),
        "target": str(target or "").strip(),
        "execution_channel": "narrative_embodied",
        "consent_status": runtime.consent_status,
    }
    outcome = runtime.service.process(
        request,
        source_event_id=runtime.source_event_id,
        operation_id=f"somatic_tool:{runtime.service.thread_id}:{tool_call_id}",
        source_kind="agent_action",
        event_context={
            "actor": "agent",
            "target": request["target"],
            "action_description": request["action_description"],
            "execution_channel": "narrative_embodied",
        },
    )
    status = "success" if outcome.status == "completed" else outcome.status
    full_payload = {
        "operation": "somatic_action",
        "status": status,
        "execution_channel": "narrative_embodied",
        "outcome": outcome.to_dict(),
        "source_boundary": "结果只描述当前执行通道中的动作和身体反馈，通道能力以能力快照为准。",
    }
    # content 是模型可见结果：只给执行状态、动作反馈和内感受，不给身体
    # 状态数值或 revision。artifact 是框架内部记录，供 ExperienceSlice
    # 保存完整状态，不能作为模型上下文使用。
    public_outcome = {
        key: full_payload["outcome"].get(key)
        for key in (
            "status",
            "operation_id",
            "source_kind",
            "visible_reaction",
            "interoception_description",
            "action_description",
            "error",
            "duplicate",
        )
    }
    public_payload = {
        "operation": "somatic_action",
        "status": status,
        "execution_channel": "narrative_embodied",
        "outcome": public_outcome,
        "source_boundary": full_payload["source_boundary"],
    }
    return _message(tool_call_id, public_payload, artifact=full_payload)


__all__ = ["somatic_action", "somatic_tool_context"]
