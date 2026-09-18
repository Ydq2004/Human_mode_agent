"""模拟身体模块的通用协议与纯规则计算。

这是框架层的第一步基础设施：事件是否发生必须由结构化请求明确说明；
LLM 或上层模块只能提供方向和幅度，最终状态由这里校验并限制范围。
本模块不调用 LLM、不写数据库，也不包含成人专用身体字段。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from math import exp
from typing import Any, Mapping as TypingMapping
import re

from core.experience import AgentAction, create_agent_action
from core.perception import PerceptionEvent, create_perception_event


SOMATIC_EVENT_STATUSES = frozenset({"occurred", "rejected", "uncertain"})
SOMATIC_INTENSITIES = frozenset({"gentle", "moderate", "strong", "unknown"})
SOMATIC_DURATIONS = frozenset({"brief", "sustained", "unknown"})
SOMATIC_CONTACT_TYPES = frozenset({"stroke", "press", "friction", "hold", "touch", "action", "other"})
SOMATIC_MOVEMENTS = frozenset({"still", "repeated", "moving", "unknown"})
BODY_STATE_DELTA = {
    "slight": 0.05,
    "moderate": 0.10,
    "significant": 0.15,
    "intense": 0.20,
}
_DIRECTIONS = frozenset({"increase", "decrease", "unchanged"})
CONSENT_STATUSES = frozenset({"explicit_yes", "explicit_no", "unknown", "not_applicable"})

# 这是通用框架的中性部位注册表。领域扩展只能在实例配置中显式注册，
# 不能通过在这里添加特殊部位来改变所有角色的默认能力。
GENERIC_REGION_PROFILES = {
    "head_scalp": {"baseline_sensitivity": 0.5, "recovery_half_life_minutes": 45.0},
    "face": {"baseline_sensitivity": 0.45, "recovery_half_life_minutes": 35.0},
    "neck": {"baseline_sensitivity": 0.5, "recovery_half_life_minutes": 40.0},
    "shoulder": {"baseline_sensitivity": 0.35, "recovery_half_life_minutes": 30.0},
    "arm": {"baseline_sensitivity": 0.4, "recovery_half_life_minutes": 30.0},
    "arms": {"baseline_sensitivity": 0.4, "recovery_half_life_minutes": 30.0},
    "hand": {"baseline_sensitivity": 0.45, "recovery_half_life_minutes": 25.0},
    "back": {"baseline_sensitivity": 0.35, "recovery_half_life_minutes": 40.0},
    "thigh": {"baseline_sensitivity": 0.45, "recovery_half_life_minutes": 45.0},
}
GENERIC_REGION_ALIASES = {
    "头": "head_scalp", "头部": "head_scalp", "头发": "head_scalp", "头皮": "head_scalp",
    "脸": "face", "脸部": "face",
    "脖子": "neck", "颈部": "neck",
    "肩": "shoulder", "肩膀": "shoulder",
    "手臂": "arm", "胳膊": "arm", "手": "hand",
    "背": "back", "背部": "back", "大腿": "thigh",
}

_INTENSITY_FACTOR = {"gentle": 0.35, "moderate": 0.6, "strong": 1.0, "unknown": 0.0}
_DURATION_FACTOR = {"brief": 0.35, "sustained": 0.8, "unknown": 0.0}


@dataclass(frozen=True)
class BodyRegionProfile:
    """一个中性身体部位的静态能力配置。"""

    region_id: str
    baseline_sensitivity: float = 0.5
    recovery_half_life_minutes: float = 45.0
    sensitivity_gain: float = 1.0
    activation_gain: float = 1.0

    def __post_init__(self) -> None:
        if not str(self.region_id).strip():
            raise ValueError("region_id 不能为空")
        if self.recovery_half_life_minutes <= 0:
            raise ValueError("recovery_half_life_minutes 必须大于 0")
        for name in ("baseline_sensitivity", "sensitivity_gain", "activation_gain"):
            value = float(getattr(self, name))
            if name == "baseline_sensitivity" and not 0 <= value <= 1:
                raise ValueError("baseline_sensitivity 必须在 0 到 1 之间")
            if name != "baseline_sensitivity" and value < 0:
                raise ValueError(f"{name} 不能为负数")


@dataclass(frozen=True)
class BodyRegionState:
    """单个部位的运行时状态；不包含任何领域专用字段。"""

    region_id: str
    short_term_sensitivity: float = 0.5
    activation: float = 0.0
    comfort: float = 0.5
    discomfort: float = 0.0
    local_fatigue: float = 0.0
    adaptation: float = 0.0
    last_stimulated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "short_term_sensitivity": round(max(0.0, min(1.0, float(self.short_term_sensitivity))), 6),
            "activation": round(max(0.0, min(1.0, float(self.activation))), 6),
            "comfort": round(max(0.0, min(1.0, float(self.comfort))), 6),
            "discomfort": round(max(0.0, min(1.0, float(self.discomfort))), 6),
            "local_fatigue": round(max(0.0, min(1.0, float(self.local_fatigue))), 6),
            "adaptation": round(max(0.0, min(1.0, float(self.adaptation))), 6),
            "last_stimulated_at": self.last_stimulated_at,
        }


@dataclass(frozen=True)
class SomaticContact:
    region_id: str
    contact_type: str = "touch"
    intensity: str = "unknown"
    duration: str = "unknown"
    movement: str = "unknown"
    certainty: str = "high"

    def __post_init__(self) -> None:
        if not str(self.region_id).strip():
            raise ValueError("region_id 不能为空")
        if self.contact_type not in SOMATIC_CONTACT_TYPES:
            raise ValueError(f"未知接触方式：{self.contact_type}")
        if self.intensity not in SOMATIC_INTENSITIES:
            raise ValueError(f"未知接触强度：{self.intensity}")
        if self.duration not in SOMATIC_DURATIONS:
            raise ValueError(f"未知持续时间：{self.duration}")
        if self.movement not in SOMATIC_MOVEMENTS:
            raise ValueError(f"未知移动方式：{self.movement}")

    def to_dict(self) -> dict[str, str]:
        return {
            "region_id": self.region_id,
            "contact_type": self.contact_type,
            "intensity": self.intensity,
            "duration": self.duration,
            "movement": self.movement,
            "certainty": str(self.certainty or "high"),
        }


def _region_profile(region_id: str, registry: TypingMapping[str, Any] | None = None) -> BodyRegionProfile | None:
    source = GENERIC_REGION_PROFILES if registry is None else registry
    canonical_id = GENERIC_REGION_ALIASES.get(region_id, region_id)
    raw = source.get(canonical_id)
    if raw is None:
        return None
    if isinstance(raw, BodyRegionProfile):
        return raw
    return BodyRegionProfile(region_id=canonical_id, **dict(raw))


def normalize_region_id(region_id: Any) -> str:
    value = str(region_id or "").strip()
    return GENERIC_REGION_ALIASES.get(value, value)

def infer_explicit_consent(text: Any) -> str:
    """只识别当前用户事件中的明确同意/拒绝短语；不从暧昧语气推断同意。"""
    value = str(text or "").strip().lower()
    if not value:
        return "unknown"
    if re.search(r"不同意|不允许|不要这样|别这样|停止|停下|no\b|拒绝", value):
        return "explicit_no"
    if re.search(r"我同意|明确同意|可以抱|请抱|允许你|请继续|好的，?可以|yes\b", value):
        return "explicit_yes"
    return "unknown"


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
    consent_status: str = "unknown"
    contacts: tuple[SomaticContact, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in SOMATIC_EVENT_STATUSES:
            raise ValueError(f"未知的身体事件状态：{self.status}")
        for name in ("contact_object", "contact_area", "uncertainty", "reason"):
            object.__setattr__(self, name, str(getattr(self, name) or "").strip())
        if self.intensity not in SOMATIC_INTENSITIES:
            raise ValueError(f"未知的接触强度：{self.intensity}")
        if self.duration not in SOMATIC_DURATIONS:
            raise ValueError(f"未知的持续时间：{self.duration}")
        if self.consent_status not in CONSENT_STATUSES:
            raise ValueError(f"未知的同意状态：{self.consent_status}")
        object.__setattr__(self, "contacts", tuple(self.contacts or ()))

    def to_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "contact_object": self.contact_object,
            "contact_area": self.contact_area,
            "intensity": self.intensity,
            "duration": self.duration,
            "uncertainty": self.uncertainty,
            "reason": self.reason,
            "consent_status": self.consent_status,
            "contacts": [contact.to_dict() for contact in self.contacts],
        }


@dataclass(frozen=True)
class SomaticState:
    """可扩展的身体状态快照；当前只提供通用 0 到 1 数值。"""

    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        cleaned: dict[str, Any] = {}
        for key, value in dict(self.values or {}).items():
            name = str(key).strip()
            if not name:
                continue
            if name in {
                "regions",
                "derived",
                "extension_state",
                "legacy_global",
                "schema_version",
                "last_activity_at",
            }:
                if name in {"regions", "derived", "extension_state", "legacy_global"} and value is not None and not isinstance(value, Mapping):
                    raise ValueError(f"身体状态 {name} 必须是 JSON object")
                cleaned[name] = deepcopy(value)
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"身体状态 {name} 必须是数字") from exc
            cleaned[name] = max(0.0, min(1.0, number))
        object.__setattr__(self, "values", cleaned)

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(dict(self.values))

    def region_states(self) -> dict[str, BodyRegionState]:
        raw_regions = self.values.get("regions") if isinstance(self.values, Mapping) else None
        if not isinstance(raw_regions, Mapping):
            return {}
        result: dict[str, BodyRegionState] = {}
        for region_id, raw in raw_regions.items():
            if not isinstance(raw, Mapping):
                continue
            payload = dict(raw)
            payload.pop("region_id", None)
            result[str(region_id)] = BodyRegionState(region_id=str(region_id), **payload)
        return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def decay_somatic_state(
    state: SomaticState,
    *,
    now: datetime | None = None,
    reference_time: datetime | None = None,
    region_registry: TypingMapping[str, Any] | None = None,
) -> SomaticState:
    """返回恢复后的临时视图；不会写库，也不会改变 revision。"""
    regions = state.region_states()
    if not regions:
        return state
    current_time = now or datetime.now(timezone.utc)
    updated: dict[str, Any] = {}
    for region_id, region in regions.items():
        profile = _region_profile(region_id, region_registry) or BodyRegionProfile(region_id)
        stamp = region.last_stimulated_at
        try:
            last = datetime.fromisoformat(stamp) if stamp else (reference_time or current_time)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            elapsed_minutes = max(0.0, (current_time - last).total_seconds() / 60.0)
        except (TypeError, ValueError):
            elapsed_minutes = 0.0
        factor = exp(-elapsed_minutes / (profile.recovery_half_life_minutes / 0.69314718056))
        baseline = profile.baseline_sensitivity
        updated[region_id] = BodyRegionState(
            region_id=region_id,
            short_term_sensitivity=baseline + (region.short_term_sensitivity - baseline) * factor,
            activation=region.activation * factor,
            comfort=region.comfort,
            discomfort=region.discomfort * factor,
            local_fatigue=region.local_fatigue * factor,
            adaptation=region.adaptation,
            last_stimulated_at=region.last_stimulated_at,
        ).to_dict()
    values = state.to_dict()
    values["regions"] = updated
    values["derived"] = _derive_global_state(updated)
    return SomaticState(values)


def _derive_global_state(regions: Mapping[str, Any]) -> dict[str, float]:
    if not regions:
        return {}
    values = list(regions.values())
    return {
        "arousal": round(sum(float(item.get("activation", 0.0)) for item in values) / len(values), 6),
        "sensitivity": round(sum(float(item.get("short_term_sensitivity", 0.0)) for item in values) / len(values), 6),
    }


def _text(value: Any, *, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]


def _parse_contacts(request: Mapping[str, Any], region_registry: TypingMapping[str, Any] | None = None) -> tuple[SomaticContact, ...]:
    raw_contacts = request.get("contacts")
    if raw_contacts is None:
        area = _text(request.get("contact_area"))
        if not area:
            return ()
        raw_contacts = [{
            "region_id": area,
            "contact_type": request.get("contact_type", "touch"),
            "intensity": request.get("intensity", "unknown"),
            "duration": request.get("duration", "unknown"),
            "movement": request.get("movement", "unknown"),
            "certainty": request.get("uncertainty", "high"),
        }]
    if not isinstance(raw_contacts, list) or not raw_contacts:
        return ()
    parsed: list[SomaticContact] = []
    for raw in raw_contacts[:8]:
        if not isinstance(raw, Mapping):
            continue
        region_id = normalize_region_id(raw.get("region_id") or raw.get("contact_area"))
        if not region_id or _region_profile(region_id, region_registry) is None:
            continue
        try:
            parsed.append(SomaticContact(
                region_id=region_id,
                contact_type=_text(raw.get("contact_type")).lower() or "touch",
                intensity=_text(raw.get("intensity") or raw.get("pressure")).lower() or "unknown",
                duration=_text(raw.get("duration")).lower() or "unknown",
                movement=_text(raw.get("movement")).lower() or "unknown",
                certainty=_text(raw.get("certainty") or raw.get("uncertainty")).lower() or "high",
            ))
        except ValueError:
            continue
    return tuple(parsed)


def adjudicate_somatic_event(
    request: Mapping[str, Any] | None,
    *,
    region_registry: TypingMapping[str, Any] | None = None,
) -> SomaticEventDecision:
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
    consent_status = _text(request.get("consent_status") or request.get("consent")).lower() or "unknown"
    if consent_status not in CONSENT_STATUSES:
        return SomaticEventDecision("uncertain", contact_object, contact_area, intensity, duration, "high", "consent_status 不在允许范围内。", consent_status=consent_status)
    if intensity not in SOMATIC_INTENSITIES or duration not in SOMATIC_DURATIONS:
        return SomaticEventDecision("uncertain", contact_object, contact_area, reason="接触强度或持续时间不在允许范围内。", consent_status=consent_status)
    if not confirmed:
        return SomaticEventDecision("rejected", contact_object, contact_area, intensity, duration, uncertainty, "请求明确表示模拟事件没有发生。", consent_status=consent_status)
    if consent_status == "explicit_no":
        return SomaticEventDecision("rejected", contact_object, contact_area, intensity, duration, uncertainty, "请求明确表示对该动作不同意。", consent_status=consent_status)
    contacts = _parse_contacts(request, region_registry)
    if not contact_object or not contacts:
        return SomaticEventDecision("uncertain", contact_object, contact_area, intensity, duration, "high", "已声明事件发生，但缺少对象或有效部位。", consent_status=consent_status)
    return SomaticEventDecision("occurred", contact_object, contacts[0].region_id, intensity, duration, uncertainty, "结构化请求明确确认事件发生。", consent_status=consent_status, contacts=contacts)


def apply_somatic_appraisal(
    state: SomaticState | Mapping[str, float],
    appraisal: Mapping[str, Any] | None,
    *,
    contacts: tuple[SomaticContact, ...] | list[SomaticContact] = (),
    now: datetime | None = None,
    region_registry: TypingMapping[str, Any] | None = None,
) -> tuple[SomaticState, dict[str, float]]:
    """把方向和幅度转换为有限状态变化，拒绝直接写入绝对值。"""
    current = state if isinstance(state, SomaticState) else SomaticState(state)
    if not isinstance(appraisal, Mapping):
        return current, {}
    # 调用方若传入 now，表示要求在本次变更前物化恢复；服务层已经提供
    # 恢复后的 current_state 时不再重复衰减。
    if now is not None:
        current = decay_somatic_state(current, now=now, region_registry=region_registry)
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

    contacts = tuple(contacts or ())
    if contacts:
        region_values = {
            region_id: item.to_dict()
            for region_id, item in current.region_states().items()
        }
        region_updates = appraisal.get("region_updates")
        update_by_region = {
            str(item.get("region_id")): item
            for item in (region_updates if isinstance(region_updates, list) else [])
            if isinstance(item, Mapping) and item.get("region_id")
        }
        # 旧版评价没有 region_updates 时，使用旧版方向作为兼容输入，
        # 但绝对数值仍由这里的规则计算。
        fallback_direction = _text(appraisal.get("sensitivity_change")).lower() or "unchanged"
        fallback_magnitude = _text(appraisal.get("sensitivity_magnitude")).lower() or "slight"
        for contact in contacts:
            profile = _region_profile(contact.region_id, region_registry) or BodyRegionProfile(contact.region_id)
            raw = region_values.get(contact.region_id)
            if raw is None:
                raw = BodyRegionState(
                    region_id=contact.region_id,
                    short_term_sensitivity=profile.baseline_sensitivity,
                ).to_dict()
            update = update_by_region.get(contact.region_id, {})
            direction = _text(update.get("sensation_direction") or update.get("direction")).lower() or fallback_direction
            magnitude = _text(update.get("sensation_magnitude") or update.get("magnitude")).lower() or fallback_magnitude
            if direction not in _DIRECTIONS or magnitude not in BODY_STATE_DELTA:
                direction, magnitude = "unchanged", "slight"
            stimulus = _INTENSITY_FACTOR.get(contact.intensity, 0.0) * _DURATION_FACTOR.get(contact.duration, 0.0)
            delta = BODY_STATE_DELTA[magnitude] * stimulus * profile.sensitivity_gain
            if direction == "decrease":
                delta = -delta
            elif direction == "unchanged":
                delta = 0.0
            sensitivity = _clamp(float(raw.get("short_term_sensitivity", profile.baseline_sensitivity)) + delta)
            activation_delta = BODY_STATE_DELTA[magnitude] * stimulus * profile.activation_gain
            activation = _clamp(float(raw.get("activation", 0.0)) + (activation_delta if direction != "decrease" else -activation_delta))
            fatigue = _clamp(float(raw.get("local_fatigue", 0.0)) + stimulus * 0.08)
            adaptation = _clamp(float(raw.get("adaptation", 0.0)) + (0.002 if sensitivity >= 0.7 and stimulus > 0 else 0.0))
            region_values[contact.region_id] = BodyRegionState(
                region_id=contact.region_id,
                short_term_sensitivity=sensitivity,
                activation=activation,
                comfort=float(raw.get("comfort", 0.5)),
                discomfort=float(raw.get("discomfort", 0.0)),
                local_fatigue=fatigue,
                adaptation=adaptation,
                last_stimulated_at=(now or datetime.now(timezone.utc)).isoformat(),
            ).to_dict()
        values["schema_version"] = 2
        values["regions"] = region_values
        values["derived"] = _derive_global_state(region_values)
        values["last_activity_at"] = (now or datetime.now(timezone.utc)).isoformat()
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


__all__ = [
    "BODY_STATE_DELTA",
    "CONSENT_STATUSES",
    "GENERIC_REGION_PROFILES",
    "BodyRegionProfile",
    "BodyRegionState",
    "SomaticContact",
    "SomaticEventDecision",
    "SomaticState",
    "adjudicate_somatic_event",
    "apply_somatic_appraisal",
    "create_somatic_interoception",
    "create_somatic_response",
    "decay_somatic_state",
    "infer_explicit_consent",
    "normalize_region_id",
]
