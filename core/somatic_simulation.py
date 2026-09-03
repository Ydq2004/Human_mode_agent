"""模拟身体模块的通用协议与纯规则计算。

这是框架层的第一步基础设施：事件是否发生必须由结构化请求明确说明；
LLM 或上层模块只能提供方向和幅度，最终状态由这里校验并限制范围。
本模块不调用 LLM、不写数据库，也不包含成人专用身体字段。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from core.experience import AgentAction, create_agent_action
from core.perception import PerceptionEvent, create_perception_event


SOMATIC_EVENT_STATUSES = frozenset({"occurred", "rejected", "uncertain"})
SOMATIC_INTENSITIES = frozenset({"gentle", "moderate", "strong", "unknown"})
SOMATIC_DURATIONS = frozenset({"brief", "sustained", "unknown"})
BODY_STATE_DELTA = {
    "slight": 0.05,
    "moderate": 0.10,
    "significant": 0.15,
    "intense": 0.20,
}
_DIRECTIONS = frozenset({"increase", "decrease", "unchanged"})


@dataclass(frozen=True)
class SomaticEventDecision:
    """对一次模拟身体事件的结构化裁决。"""

    status: str
    contact_object: str = ""
    contact_area: str = ""
    intensity: str = "unknown"
    duration: str = "unknown"
    uncertainty: str = "high"
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in SOMATIC_EVENT_STATUSES:
            raise ValueError(f"未知的身体事件状态：{self.status}")
        for name in ("contact_object", "contact_area", "uncertainty", "reason"):
            object.__setattr__(self, name, str(getattr(self, name) or "").strip())
        if self.intensity not in SOMATIC_INTENSITIES:
            raise ValueError(f"未知的接触强度：{self.intensity}")
        if self.duration not in SOMATIC_DURATIONS:
            raise ValueError(f"未知的持续时间：{self.duration}")

    def to_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "contact_object": self.contact_object,
            "contact_area": self.contact_area,
            "intensity": self.intensity,
            "duration": self.duration,
            "uncertainty": self.uncertainty,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SomaticState:
    """可扩展的身体状态快照；当前只提供通用 0 到 1 数值。"""

    values: Mapping[str, float]

    def __post_init__(self) -> None:
        cleaned: dict[str, float] = {}
        for key, value in dict(self.values or {}).items():
            name = str(key).strip()
            if not name:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"身体状态 {name} 必须是数字") from exc
            cleaned[name] = max(0.0, min(1.0, number))
        object.__setattr__(self, "values", cleaned)

    def to_dict(self) -> dict[str, float]:
        return deepcopy(dict(self.values))


def _text(value: Any, *, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]


def adjudicate_somatic_event(request: Mapping[str, Any] | None) -> SomaticEventDecision:
    """只依据结构化请求裁决事件，不从自由文本猜测接触发生。"""
    if not isinstance(request, Mapping):
        return SomaticEventDecision("uncertain", reason="缺少结构化身体事件请求，不能确认事件是否发生。")
    confirmed = request.get("event_confirmed")
    if not isinstance(confirmed, bool):
        return SomaticEventDecision("uncertain", reason="event_confirmed 不是明确的布尔值。")

    contact_object = _text(request.get("contact_object"))
    contact_area = _text(request.get("contact_area"))
    intensity = _text(request.get("intensity")).lower() or "unknown"
    duration = _text(request.get("duration")).lower() or "unknown"
    uncertainty = _text(request.get("uncertainty")).lower() or "high"
    if intensity not in SOMATIC_INTENSITIES or duration not in SOMATIC_DURATIONS:
        return SomaticEventDecision("uncertain", contact_object, contact_area, reason="接触强度或持续时间不在允许范围内。")
    if not confirmed:
        return SomaticEventDecision("rejected", contact_object, contact_area, intensity, duration, uncertainty, "请求明确表示模拟事件没有发生。")
    if not contact_object or not contact_area:
        return SomaticEventDecision("uncertain", contact_object, contact_area, intensity, duration, "high", "已声明事件发生，但缺少对象或部位。")
    return SomaticEventDecision("occurred", contact_object, contact_area, intensity, duration, uncertainty, "结构化请求明确确认事件发生。")


def apply_somatic_appraisal(
    state: SomaticState | Mapping[str, float],
    appraisal: Mapping[str, Any] | None,
) -> tuple[SomaticState, dict[str, float]]:
    """把方向和幅度转换为有限状态变化，拒绝直接写入绝对值。"""
    current = state if isinstance(state, SomaticState) else SomaticState(state)
    if not isinstance(appraisal, Mapping):
        return current, {}
    values = current.to_dict()
    changes: dict[str, float] = {}
    for dimension in ("arousal", "sensitivity"):
        direction_key = f"{dimension}_change"
        magnitude_key = f"{dimension}_magnitude"
        if direction_key not in appraisal and magnitude_key not in appraisal:
            continue
        direction = _text(appraisal.get(direction_key)).lower() or "unchanged"
        magnitude = _text(appraisal.get(magnitude_key)).lower() or "slight"
        if direction not in _DIRECTIONS or magnitude not in BODY_STATE_DELTA:
            continue
        if dimension not in values:
            values[dimension] = 0.5
        delta = BODY_STATE_DELTA[magnitude]
        if direction == "decrease":
            delta = -delta
        elif direction == "unchanged":
            delta = 0.0
        updated = max(0.0, min(1.0, values[dimension] + delta))
        changes[dimension] = round(updated - values[dimension], 4)
        values[dimension] = updated
    return SomaticState(values), changes


def create_somatic_interoception(decision: SomaticEventDecision, state: SomaticState, description: str, *, triggered_by_event: str) -> PerceptionEvent:
    """把内部感觉包装成可在当前回复前注入的感知事件。"""
    return create_perception_event(
        source="somatic_module",
        modality="interoception",
        content=_text(description, limit=500) or "身体出现了短暂的内部感觉。",
        metadata={"somatic_decision": decision.to_dict(), "body_state": state.to_dict(), "triggered_by_event": str(triggered_by_event)},
    )


def create_somatic_response(description: str, state: SomaticState, *, triggered_by_event: str) -> AgentAction:
    """把可见本能反应包装成现有经历行动。"""
    return create_agent_action(
        "somatic_response",
        _text(description, limit=500) or "身体出现了短暂的本能反应。",
        metadata={"triggered_by": "somatic_module", "triggered_by_event": str(triggered_by_event), "body_state_snapshot": state.to_dict()},
    )


__all__ = ["BODY_STATE_DELTA", "SomaticEventDecision", "SomaticState", "adjudicate_somatic_event", "apply_somatic_appraisal", "create_somatic_interoception", "create_somatic_response"]
