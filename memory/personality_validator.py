"""人格证据候选的代码校验。

LLM 以后只能提供候选；本模块负责确认来源、时间跨度和变化边界，
不会写数据库，也不会直接修改人格。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from memory.experience_store import get_experience_slice
from memory.personality_traits import get_trait_definition

MIN_EVIDENCE_COUNT = 3
MIN_TIME_SPAN_DAYS = 3
MAX_REFLECTION_CHANGE = 5.0
VALID_DIRECTIONS = {"increase", "decrease"}


class EvidenceValidationError(ValueError):
    pass


def _ids(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def adjust_confidence_for_counter_evidence(evidence: dict[str, Any]) -> float:
    positive = len(_ids(evidence.get("evidence_slice_ids")))
    counter = len(_ids(evidence.get("counter_evidence_slice_ids")))
    if positive < 1:
        return 0.0
    ratio = counter / positive
    if ratio >= 0.8:
        raise EvidenceValidationError("反面证据过强")
    confidence = float(evidence.get("confidence", 0.5))
    return max(0.1, confidence * (1.0 - ratio * 0.5))


def validate_trait_evidence(
    evidence: dict[str, Any],
    *,
    persona_instance_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise EvidenceValidationError("证据候选必须是对象")
    trait_id = str(evidence.get("trait_id") or "").strip()
    if get_trait_definition(trait_id) is None:
        raise EvidenceValidationError(f"未知特质 ID: {trait_id}")
    direction = str(evidence.get("direction") or "").strip()
    if direction not in VALID_DIRECTIONS:
        raise EvidenceValidationError("direction 必须是 increase 或 decrease")

    evidence_ids = _ids(evidence.get("evidence_slice_ids"))
    if len(evidence_ids) < MIN_EVIDENCE_COUNT:
        raise EvidenceValidationError(
            f"证据不足：需要至少 {MIN_EVIDENCE_COUNT} 条经历，实际 {len(evidence_ids)}"
        )
    counter_ids = _ids(evidence.get("counter_evidence_slice_ids"))
    if set(evidence_ids) & set(counter_ids):
        raise EvidenceValidationError("同一切片不能同时作为正面和反面证据")

    slices = []
    for slice_id in evidence_ids + counter_ids:
        stored = get_experience_slice(slice_id)
        if stored is None:
            raise EvidenceValidationError(f"切片 {slice_id} 不存在")
        slices.append(stored)

    try:
        # 形成该结论的时间跨度只能由正向证据提供；反面证据不能被用来
        # 凑够跨度，否则一条很早的无关反例会伪造“长期模式”。
        positive_slices = [get_experience_slice(slice_id) for slice_id in evidence_ids]
        timestamps = [
            datetime.fromisoformat(item.experience.perception_event.occurred_at)
            for item in positive_slices if item is not None
        ]
    except (TypeError, ValueError) as exc:
        raise EvidenceValidationError("证据切片时间无效") from exc
    if max(timestamps) - min(timestamps) < timedelta(days=MIN_TIME_SPAN_DAYS):
        raise EvidenceValidationError(f"时间跨度不足：至少需要 {MIN_TIME_SPAN_DAYS} 天")

    strength = float(evidence.get("strength", 0.0))
    confidence = float(evidence.get("confidence", 0.5))
    if not 0.0 <= strength <= 1.0 or not 0.0 <= confidence <= 1.0:
        raise EvidenceValidationError("strength 和 confidence 必须在 0~1 之间")
    adjusted_confidence = adjust_confidence_for_counter_evidence(evidence)
    return {
        "trait_id": trait_id,
        "direction": direction,
        "strength": strength,
        "scope": str(evidence.get("scope") or "general").strip() or "general",
        "behavior_source": str(evidence.get("behavior_source") or "unknown").strip() or "unknown",
        "evidence_slice_ids": tuple(evidence_ids),
        "counter_evidence_slice_ids": tuple(counter_ids),
        "confidence": adjusted_confidence,
        "uncertainty": str(evidence.get("uncertainty") or "").strip(),
        "persona_instance_id": str(persona_instance_id or evidence.get("persona_instance_id") or "default_instance"),
        "status": "accepted",
    }


def validate_reflection_batch(
    evidences: list[dict[str, Any]],
    *,
    persona_instance_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    valid: list[dict[str, Any]] = []
    rejected: list[str] = []
    for item in evidences:
        try:
            valid.append(validate_trait_evidence(item, persona_instance_id=persona_instance_id))
        except (EvidenceValidationError, TypeError, ValueError) as exc:
            rejected.append(f"{item.get('trait_id', 'unknown')}: {exc}")

    changes: dict[str, float] = {}
    for item in valid:
        trait = get_trait_definition(item["trait_id"])
        direction = 1.0 if item["direction"] == "increase" else -1.0
        changes[item["trait_id"]] = changes.get(item["trait_id"], 0.0) + (
            direction * item["strength"] * item["confidence"] * trait.trait_delta_scale
        )
    oversized = {trait_id for trait_id, change in changes.items() if abs(change) > MAX_REFLECTION_CHANGE}
    for trait_id in oversized:
        rejected.append(f"{trait_id}: 单次反思变化过大")
    valid = [item for item in valid if item["trait_id"] not in oversized]
    return valid, rejected
