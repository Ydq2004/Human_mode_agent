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


_RUNTIME: ContextVar[_ToolRuntime | None] = ContextVar("somatic_tool_runtime", default=None)


@contextmanager
def somatic_tool_context(
    service: SomaticService,
    *,
    source_event_id: str,
) -> Iterator[None]:
    """把当前实例身体服务绑定到一次 Agent 调用，结束后立即清理。"""
    token = _RUNTIME.set(_ToolRuntime(service=service, source_event_id=source_event_id))
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
    """在角色世界中执行一次身体动作，并取得真实的内感受与外在反馈。

    仅当你确实决定做出身体动作时调用。参数分别描述动作、目标、接触部位、
    强度（gentle/moderate/strong）和持续时间（brief/sustained）。
    工具成功只代表角色世界中的动作成立，不代表现实设备产生物理动作。
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
        "source_boundary": (
            "动作和反馈发生在当前角色世界；现实设备是否执行物理动作由独立设备能力决定。"
        ),
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
