"""把身份裁决结果转换成可写入的一跳关系。

这是框架层的纯函数模块：它不调用 LLM、不检索数据库，也不执行写入。
身份语义由 resolver/judge 提供；本模块只负责保护关系表的结构边界：
端点方向、对称关系的规范化、非法组合的保守降级，以及稳定 relation_id。
"""

import hashlib

from memory.schema import MemoryRelation


_DIRECTIONAL_TYPES = {"belongs_to", "refers_to"}
_VALID_DIRECTIONS = {
    "candidate_to_existing",
    "existing_to_candidate",
    "symmetric",
}


def _stable_relation_id(
    source_concept_id: str,
    relation_type: str,
    target_concept_id: str,
) -> str:
    """同一组端点和关系类型始终得到同一个关系 ID，支持进程内幂等。"""
    raw = f"{source_concept_id}|{relation_type}|{target_concept_id}".encode("utf-8")
    return "rel_" + hashlib.sha256(raw).hexdigest()[:24]


def build_relation_for_candidate(
    candidate_concept_id: str,
    existing_concept_id: str,
    relation_type: str,
    relation_direction: str,
) -> MemoryRelation | None:
    """依据 judge 结果生成关系对象；无法诚实表达时返回保守结果。

    `candidate_to_existing` 和 `existing_to_candidate` 只表示候选与本轮
    已有实体的语义方向，不是数据库的固定写入顺序。对称关系按 concept_id
    排序，保证输入顺序变化不会产生两条重复边。方向性关系若缺少明确方向，
    降级为 `related_to + symmetric`，而不是凭空猜测。
    """
    candidate = str(candidate_concept_id or "").strip()
    existing = str(existing_concept_id or "").strip()
    relation_type = str(relation_type or "").strip().lower()
    direction = str(relation_direction or "").strip().lower()

    if not candidate or not existing or candidate == existing:
        return None
    if relation_type not in {"related_to", "belongs_to", "refers_to", "similar_to"}:
        return None

    if relation_type == "similar_to":
        direction = "symmetric"

    if relation_type in _DIRECTIONAL_TYPES and direction not in {
        "candidate_to_existing",
        "existing_to_candidate",
    }:
        relation_type = "related_to"
        direction = "symmetric"
    elif direction not in _VALID_DIRECTIONS:
        direction = "symmetric"

    if direction == "candidate_to_existing":
        source, target = candidate, existing
    elif direction == "existing_to_candidate":
        source, target = existing, candidate
    else:
        source, target = sorted((candidate, existing))

    return MemoryRelation(
        relation_id=_stable_relation_id(source, relation_type, target),
        source_concept_id=source,
        relation_type=relation_type,
        target_concept_id=target,
        confidence=1.0,
    )
