from dataclasses import dataclass, field


from memory.schema import MemoryEntity
from memory.sql_store import get_entity, search_by_name_or_alias, list_entities


@dataclass
class ResolveResult:
    decision: str                  # same / ambiguous / new
    target_concept_id: str | None
    confidence: float
    reason: str
    candidate_concept_ids: list[str] = field(default_factory=list)

def _norm(value) -> str:
    """统一清洗字符串，避免空格影响匹配。"""
    return str(value or "").strip()


def _signature_value(signature: dict, key: str) -> str:
    """安全读取 identity_signature 的字段。"""
    if not isinstance(signature, dict):
        return ""
    return _norm(signature.get(key))




def _same_core_signature(a: dict, b: dict) -> bool:
    """
    判断两个签名的核心身份是否一致。

    Phase 0 的核心身份先看 subject / relation / object。
    qualifier 只作为附加限定，不作为第一层硬门槛。
    """
    return (
        _signature_value(a, "subject") == _signature_value(b, "subject")
        and _signature_value(a, "relation") == _signature_value(b, "relation")
        and _signature_value(a, "object") == _signature_value(b, "object")
    )


def _same_qualifier(a: dict, b: dict) -> bool:
    """规则层只接受完全相同或双方都为空的 qualifier。"""
    qa = _signature_value(a, "qualifier")
    qb = _signature_value(b, "qualifier")
    return qa == qb

def _memory_type_compatible(candidate_type: str, entity_type: str) -> bool:
    """
    same 判断需要类型兼容。

    Phase 0 先保守处理：
    - 类型相同：可 same
    - 候选没给类型：不拦截
    - 类型不同：不直接 same，只能进入 ambiguous / related 判断
    """
    candidate_type = _norm(candidate_type)
    entity_type = _norm(entity_type)

    if not candidate_type:
        return True

    return candidate_type == entity_type


def _maybe_related_signature(a: dict, b: dict) -> bool:
    """
    判断两个认知是否相关但不相同。

    这里不要太激进，只抓最明确的相关：
    - subject 相同但 object 不同：同一主体的不同认知
    - object 相同但 subject 不同：围绕同一对象的不同认知
    """
    subject_a = _signature_value(a, "subject")
    subject_b = _signature_value(b, "subject")
    object_a = _signature_value(a, "object")
    object_b = _signature_value(b, "object")

    if subject_a and subject_a == subject_b:
        return True
    if object_a and object_a == object_b:
        return True
    return False


def _candidate_matches(candidate: dict, entity: MemoryEntity) -> ResolveResult | None:
    candidate_sig = candidate.get("identity_signature", {})
    entity_sig = entity.identity_signature or {}
    candidate_type = _norm(candidate.get("memory_type"))
    entity_type = _norm(entity.memory_type)

    if _same_core_signature(candidate_sig, entity_sig):
        if _same_qualifier(candidate_sig, entity_sig) and _memory_type_compatible(candidate_type, entity_type):
            return ResolveResult(
                decision="same",
                target_concept_id=entity.concept_id,
                confidence=0.95,
                reason="核心签名与 qualifier 完全匹配",
            )

        return ResolveResult(
            decision="ambiguous",
            target_concept_id=entity.concept_id,
            confidence=0.6,
            reason="核心签名存在重合，但 qualifier 或 memory_type 不完全一致，需要 LLM 判定",
        )

    if _maybe_related_signature(candidate_sig, entity_sig):
        return ResolveResult(
            decision="ambiguous",
            target_concept_id=entity.concept_id,
            confidence=0.5,
            reason="签名存在主体或对象交叉，需要 LLM 判定是否 related",
        )

    return None

def _choose_best_match(candidate: dict, entities: list[MemoryEntity]) -> ResolveResult | None:
    """
    从候选实体中选择身份判断。

    same 是确定结果，可以直接返回。
    ambiguous 不是确定结果，所以保留候选集合交给 LLM judge。
    """
    ambiguous_results = []

    for entity in entities:
        result = _candidate_matches(candidate, entity)
        if not result:
            continue

        if result.decision == "same":
            return result

        if result.decision == "ambiguous":
            ambiguous_results.append(result)

    if ambiguous_results:
        candidate_ids = []
        reasons = []

        for item in ambiguous_results:
            if item.target_concept_id and item.target_concept_id not in candidate_ids:
                candidate_ids.append(item.target_concept_id)
                reasons.append(f"{item.target_concept_id}: {item.reason}")

        return ResolveResult(
            decision="ambiguous",
            target_concept_id=None,
            confidence=max(item.confidence for item in ambiguous_results),
            reason="; ".join(reasons),
            candidate_concept_ids=candidate_ids,
        )

    return None

def resolve_identity(candidate: dict) -> ResolveResult:
    """
    判断候选记忆是否指向已有认知。

    candidate 建议包含：
    - target_concept_id，可选
    - concept_name
    - memory_type
    - identity_signature
    - summary
    """
    target_concept_id = _norm(candidate.get("target_concept_id"))
    if target_concept_id:
        entity = get_entity(target_concept_id)
        if entity:
            return ResolveResult(
                decision="same",
                target_concept_id=entity.concept_id,
                confidence=1.0,
                reason="target_concept_id 精确命中",
            )

    concept_name = _norm(candidate.get("concept_name"))
    all_candidates = []

    if concept_name:
        all_candidates.extend(search_by_name_or_alias(concept_name))

    memory_type = _norm(candidate.get("memory_type"))
    if memory_type:
        all_candidates.extend(list_entities(memory_type))

    # 有签名时，要允许跨类型召回 related 候选。
    # 否则 preference 永远看不到 entity，related 判断会被提前截断成 new。
    if candidate.get("identity_signature"):
        all_candidates.extend(list_entities(None))
    elif not memory_type:
        all_candidates.extend(list_entities(None)) 

    # 按 concept_id 去重，避免同一实体被 name 和 signature 两路重复召回。
    deduped_candidates = {}
    for entity in all_candidates:
        deduped_candidates[entity.concept_id] = entity

    result = _choose_best_match(candidate, list(deduped_candidates.values()))
    if result:
        return result

    return ResolveResult(
        decision="new",
        target_concept_id=None,
        confidence=0.5,
        reason="未找到可确认的已有认知",
    )