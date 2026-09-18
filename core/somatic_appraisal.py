"""身体事件的受限 LLM 评价。

LLM 只解释一次已确认身体事件可能带来的方向、幅度和感受文字。
它不能裁决同意、关系或人格，也不能直接写身体状态的绝对值。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from core.somatic_simulation import BODY_STATE_DELTA, SomaticEventDecision, SomaticState
from core.operation_timeout import run_with_timeout


_DIRECTIONS = frozenset({"increase", "decrease", "unchanged"})
_FORBIDDEN_FIELDS = frozenset({
    "arousal",
    "sensitivity",
    "consent",
    "relationship",
    "relationship_level",
    "personality",
    "personality_change",
})


@dataclass(frozen=True)
class SomaticAppraisal:
    status: str
    arousal_change: str = "unchanged"
    arousal_magnitude: str = "slight"
    sensitivity_change: str = "unchanged"
    sensitivity_magnitude: str = "slight"
    visible_reaction: str = ""
    interoception_description: str = ""
    region_updates: list[dict[str, str]] | None = None
    reason: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "arousal_change": self.arousal_change,
            "arousal_magnitude": self.arousal_magnitude,
            "sensitivity_change": self.sensitivity_change,
            "sensitivity_magnitude": self.sensitivity_magnitude,
            "visible_reaction": self.visible_reaction,
            "interoception_description": self.interoception_description,
            "region_updates": list(self.region_updates or []),
            "reason": self.reason,
            "error": self.error,
        }


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _parse_json(content: Any) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("身体评价 LLM 返回空内容")
    text = re.sub(r"^```(?:json)?\s*", "", content.strip())
    text = re.sub(r"\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("身体评价结果必须是 JSON object")
    return value


def clean_somatic_appraisal(raw: Mapping[str, Any]) -> SomaticAppraisal:
    """验证 LLM 契约；出现越权字段时整次评价作废。"""
    forbidden = _FORBIDDEN_FIELDS.intersection(raw)
    if forbidden:
        raise ValueError(f"身体评价包含越权字段：{', '.join(sorted(forbidden))}")

    cleaned: dict[str, str] = {}
    for dimension in ("arousal", "sensitivity"):
        direction = _text(raw.get(f"{dimension}_change"), 30).lower()
        magnitude = _text(raw.get(f"{dimension}_magnitude"), 30).lower()
        if direction not in _DIRECTIONS:
            raise ValueError(f"{dimension}_change 不合法")
        if magnitude not in BODY_STATE_DELTA:
            raise ValueError(f"{dimension}_magnitude 不合法")
        cleaned[f"{dimension}_change"] = direction
        cleaned[f"{dimension}_magnitude"] = magnitude

    visible = _text(raw.get("visible_reaction"))
    interoception = _text(raw.get("interoception_description"))
    if not visible or not interoception:
        raise ValueError("身体评价必须同时提供外在反应和内感受")

    region_updates: list[dict[str, str]] = []
    raw_updates = raw.get("region_updates", [])
    if raw_updates is not None:
        if not isinstance(raw_updates, list):
            raise ValueError("region_updates 必须是数组")
        for item in raw_updates[:8]:
            if not isinstance(item, Mapping) or not str(item.get("region_id") or "").strip():
                raise ValueError("region_updates 中存在无效部位")
            direction = _text(item.get("sensation_direction") or item.get("direction"), 30).lower() or "unchanged"
            magnitude = _text(item.get("sensation_magnitude") or item.get("magnitude"), 30).lower() or "slight"
            if direction not in _DIRECTIONS or magnitude not in BODY_STATE_DELTA:
                raise ValueError("region_updates 的方向或幅度不合法")
            region_updates.append({
                "region_id": _text(item.get("region_id"), 80),
                "sensation_direction": direction,
                "sensation_magnitude": magnitude,
                "valence": _text(item.get("valence"), 40),
            })

    return SomaticAppraisal(
        status="normal",
        visible_reaction=visible,
        interoception_description=interoception,
        region_updates=region_updates,
        reason=_text(raw.get("reason")),
        **cleaned,
    )


def _build_prompt(
    *,
    decision: SomaticEventDecision,
    state: SomaticState,
    event_context: Mapping[str, Any],
    persona_context: Mapping[str, Any] | None,
) -> str:
    embodiment = dict((persona_context or {}).get("embodiment") or {})
    payload = {
        "somatic_event": decision.to_dict(),
        "state_before": state.to_dict(),
        "event_context": dict(event_context),
        "embodiment_style": embodiment.get("reaction_style", "自然、克制、符合当前角色"),
    }
    return f"""
你是通用类人 Agent 框架中的身体状态评价模块。输入中的结构化事件已经由上游裁决为发生。

你的职责是把这次事件对当前身体状态的即时影响，转换成有限的变化建议和两种反馈：
1. 为输入中声明的身体维度给出变化方向和幅度。
2. 描述一条简短、可观察的外在身体反应。
3. 描述一条简短、可被 Agent 感知的内感受。

评价依据：
- 只使用事件裁决、当前状态、事件上下文和角色提供的表达偏好。
- 反馈只描述这次事件直接产生的身体层信息，不补充输入中没有的事实。
- 角色表达偏好只影响措辞，不改变事件是否发生或状态计算规则。

输出约束：
- 方向只能是 increase、decrease、unchanged；幅度只能是 slight、moderate、significant、intense。
- 只返回协议中的字段，不写身体状态绝对值，也不增加新的状态维度。
- 不替其他模块作社会关系、人格、记忆或许可判断。
- 不续写对话，不替主 Agent 回复用户。
- 如果能明确区分部位，可额外返回 region_updates；其中只能写输入事件中已有的 region_id、方向和幅度。
- 不要创建输入中不存在的部位，也不要返回任何内部状态数值或领域扩展字段。

输入：
{json.dumps(payload, ensure_ascii=False, indent=2)}

只输出 JSON object：
{{
  "arousal_change": "increase | decrease | unchanged",
  "arousal_magnitude": "slight | moderate | significant | intense",
  "sensitivity_change": "increase | decrease | unchanged",
  "sensitivity_magnitude": "slight | moderate | significant | intense",
  "visible_reaction": "角色世界中可观察到的简短身体反应",
  "interoception_description": "Agent 本轮感知到的简短内部感觉",
  "region_updates": [
    {{"region_id": "输入中的部位 ID", "sensation_direction": "increase | decrease | unchanged", "sensation_magnitude": "slight | moderate | significant | intense", "valence": "中性身体感受类别"}}
  ],
  "reason": "只解释身体层依据"
}}
""".strip()


def appraise_somatic_event(
    *,
    llm,
    decision: SomaticEventDecision,
    state: SomaticState,
    event_context: Mapping[str, Any],
    persona_context: Mapping[str, Any] | None = None,
    timeout_seconds: float | None = None,
) -> SomaticAppraisal:
    """调用身体评价 LLM；任何失败都显式返回 failed，不修改状态。"""
    if decision.status != "occurred":
        return SomaticAppraisal(
            status="failed",
            error="只有 occurred 身体事件可以评价",
        )
    try:
        prompt = _build_prompt(
            decision=decision,
            state=state,
            event_context=event_context,
            persona_context=persona_context,
        )
        response = run_with_timeout(
            lambda: llm.bind(response_format={"type": "json_object"}).invoke(prompt),
            timeout_seconds,
        )
        return clean_somatic_appraisal(_parse_json(getattr(response, "content", None)))
    except TimeoutError as exc:
        return SomaticAppraisal(status="failed", error=str(exc) or "身体评价服务超时")
    except Exception as exc:
        return SomaticAppraisal(
            status="failed",
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )


__all__ = [
    "SomaticAppraisal",
    "appraise_somatic_event",
    "clean_somatic_appraisal",
]
