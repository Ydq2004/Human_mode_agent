"""根据出生人格和已接受证据计算当前人格倾向。"""

from __future__ import annotations

import json
from typing import Any

from memory.personality_store import list_trait_evidence, get_personality_seed
from memory.personality_traits import TRAIT_REGISTRY


def _seed_traits(row: dict[str, Any]) -> dict[str, float]:
    try:
        data = json.loads(row.get("seed_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    values = data.get("traits", {}) if isinstance(data, dict) else {}
    if not isinstance(values, dict):
        return {}
    output = {}
    for trait_id, value in values.items():
        if trait_id in TRAIT_REGISTRY:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if 0.0 <= number <= 100.0:
                output[trait_id] = number
    return output


def calculate_personality_snapshot(
    persona_instance_id: str,
    *,
    as_of_timestamp: str | None = None,
) -> dict[str, float]:
    seed = get_personality_seed(persona_instance_id)
    if seed is None:
        return {}
    values = _seed_traits(seed)
    evidences = list_trait_evidence(
        persona_instance_id=persona_instance_id,
        status="accepted",
        as_of_timestamp=as_of_timestamp,
    )
    changes = {trait_id: 0.0 for trait_id in values}
    for evidence in evidences:
        if evidence.trait_id not in values:
            continue
        direction = 1.0 if evidence.direction == "increase" else -1.0
        definition = TRAIT_REGISTRY[evidence.trait_id]
        changes[evidence.trait_id] += (
            direction * evidence.strength * evidence.confidence * definition.trait_delta_scale
        )
    return {
        trait_id: max(0.0, min(100.0, round(values[trait_id] + changes[trait_id], 2)))
        for trait_id in values
    }


def render_personality_description(snapshot: dict[str, float]) -> str:
    lines = []
    for trait_id, value in snapshot.items():
        definition = TRAIT_REGISTRY.get(trait_id)
        if definition is None:
            continue
        if value < 40:
            level, description = "较低", definition.low_description
        elif value > 60:
            level, description = "较高", definition.high_description
        else:
            level, description = "中等", "处于该特质的中间范围"
        lines.append(f"- {definition.display_name}（{level}）：{description}")
    return "\n".join(lines)
