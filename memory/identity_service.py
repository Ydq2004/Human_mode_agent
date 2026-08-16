from memory.identity_judge import judge_ambiguous_identity
from memory.identity_resolver import resolve_identity
from memory.sql_store import get_entity


def resolve_identity_with_judge(candidate: dict, llm) -> dict:
    """
    编排 deterministic resolver 和 LLM judge。

    这里是框架层 glue：
    - resolver 负责候选召回和确定性判断
    - judge 只裁决 ambiguous
    - 本函数不写库，不创建关系，只返回身份判断结果
    """
    rule_result = resolve_identity(candidate)

    if rule_result.decision == "same":
        return {
            "decision": "same",
            "target_concept_id": rule_result.target_concept_id,
            "relation_type": "none",
            "relation_direction": "none",
            "reason": rule_result.reason,
        }

    if rule_result.decision == "new":
        return {
            "decision": "new",
            "target_concept_id": None,
            "relation_type": "none",
            "relation_direction": "none",
            "reason": rule_result.reason,
        }

    candidate_entities = []
    for concept_id in rule_result.candidate_concept_ids:
        entity = get_entity(concept_id)
        if entity:
            candidate_entities.append(entity)

    return judge_ambiguous_identity(candidate, candidate_entities, llm)
