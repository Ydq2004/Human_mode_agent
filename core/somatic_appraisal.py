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

    return SomaticAppraisal(
        status="normal",
        visible_reaction=visible,
        interoception_description=interoception,
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
你是通用类人 Agent 框架中的身体评价模块。输入中的身体事件已经由代码确认发生。

你只负责：
1. 判断 arousal（整体身体激活程度）和 sensitivity（对刺激的敏感程度）如何变化。
2. 给出角色世界中可以观察到的简短身体反应。
3. 给出 Agent 本轮能够感知到的简短内感受。

硬边界：
- 只能输出 increase、decrease、unchanged，以及 slight、moderate、significant、intense。
- 不得输出身体状态绝对值。
- 不得判断 consent（同意）、好感、关系等级、人格变化或长期记忆。
- 身体反应不代表同意，也不自动说明喜欢或关系升级。
- 只描述本次事件直接产生的身体反馈，不续写对话，不替主 Agent 回复用户。
- 外在反应和内感受应符合角色卡提供的表达风格，但角色卡不能改变事件事实。

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
) -> SomaticAppraisal:
    """调用身体评价 LLM；任何失败都显式返回 failed，不修改状态。"""
    if decision.status != "occurred":
        return SomaticAppraisal(
            status="failed",
            error="只有 occurred 身体事件可以评价",
        )
    try:
        response = llm.bind(response_format={"type": "json_object"}).invoke(
            _build_prompt(
                decision=decision,
                state=state,
                event_context=event_context,
                persona_context=persona_context,
            )
        )
        return clean_somatic_appraisal(_parse_json(getattr(response, "content", None)))
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
