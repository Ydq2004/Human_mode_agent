"""
ExperienceAppraisal 的统一输出契约。

这是框架层：
- 只保存一次即时评价的三个区域；
- 不计算最终 mood；
- 不写入长期记忆；
- 每个区域允许独立回退。
"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from core.experience import ExperienceSlice
from core.experience_review import fallback_experience_review
import json
import re
from config import (
    EMOTION_AFFECT_DIRECTION_DELTA,
    EMOTION_SCORE_INITIAL_BASE,
    EMOTION_SCORE_INITIAL_MAX,
    EMOTION_SCORE_INITIAL_MIN,
    EVENT_INITIAL_BIAS,
    MEMORY_INITIAL_BIAS_FACTOR,
    MOOD_BASELINE,
    MOOD_EVENT_VALENCE_BIAS,
    MOOD_INITIAL_BIAS_FACTOR,
    MOOD_SALIENCE_FACTOR,
    PERSONA_INITIAL_BIAS,
    STRENGTH_SCORE_RANGES,
)
from memory.types import ALLOWED_MEMORY_TYPES
from emotion.appraisal_rules import (
    compute_mood_impact,
    compute_existing_memory_emotion_update,
    compute_initial_memory_impression,
)



def fallback_emotion_assessment(reason: str) -> dict[str, Any]:
    return {
        "event_relevance": "none",
        "event_valence": "neutral",
        "salience": "low",
        "reason": "",
        "evidence": [],
        "affected_memories": [],
        "uncertainties": [reason],
    }


def fallback_memory_assessment(reason: str) -> dict[str, Any]:
    return {
        "memory_candidates": [],
        "new_memory_impressions": [],
        "reason": "",
        "uncertainties": [reason],
    }


@dataclass(frozen=True)
class ExperienceAppraisal:
    """一次 ExperienceSlice 的后台即时评价结果。"""

    experience_review: dict[str, Any]
    emotion_assessment: dict[str, Any]
    memory_assessment: dict[str, Any]
    fallback_regions: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experience_review": deepcopy(self.experience_review),
            "emotion_assessment": deepcopy(self.emotion_assessment),
            "memory_assessment": deepcopy(self.memory_assessment),
            "fallback_regions": list(self.fallback_regions),
        }


def compute_appraisal_effects(
    experience: ExperienceSlice,
    appraisal: ExperienceAppraisal,
    mood_reactivity: float,
) -> dict[str, Any]:
    """
    把 ExperienceAppraisal 转成代码计算结果。

    这里只计算候选，不改变运行状态，也不写长期记忆。
    """
    emotion = appraisal.emotion_assessment
    memory = appraisal.memory_assessment

    state_snapshot = dict(experience.state_snapshot)
    if "mood" not in state_snapshot:
        raise ValueError("ExperienceSlice 缺少 mood 快照")

    current_mood = float(state_snapshot["mood"])

    mood_effect = compute_mood_impact(
        emotion,
        current_mood=current_mood,
        mood_reactivity=mood_reactivity,
    )

    refs_by_id = {}

    for ref in experience.activated_memory_refs:
        concept_id = str(
            ref.get("concept_id", "")
        ).strip()

        if concept_id:
            refs_by_id[concept_id] = ref

    existing_memory_updates = []

    for affected in emotion.get(
        "affected_memories",
        [],
    ):
        concept_id = affected.get("concept_id")
        ref = refs_by_id.get(concept_id)

        if ref is None:
            continue

        try:
            current_score = float(
                ref.get("emotion_score")
            )
            update = compute_existing_memory_emotion_update(
                current_score,
                affected,
            )
        except (TypeError, ValueError):
            continue

        # AppraisalWorker 和 CommitWorker 是异步的。评价时看到的 current_score
        # 只是事件开始时的快照；等真正提交时，前序事件可能已经修改了同一
        # 条认知。因此这里只传递“如何变化”的意图，不能携带会过期的绝对
        # emotion_score / emotion_label。score_delta 仅供调试观察本次快照下
        # 的预期幅度，提交层仍必须按数据库当前值重新计算。
        existing_memory_updates.append({
            "concept_id": concept_id,
            "change_direction": affected.get("change_direction"),
            "label_update": deepcopy(affected.get("label_update")),
            "score_delta": update["score_delta"],
        })

    new_memory_impressions = []

    for impression in memory.get(
        "new_memory_impressions",
        [],
    ):
        try:
            computed = compute_initial_memory_impression(
                impression=impression,
                event_valence=emotion["event_valence"],
                activated_memory_refs=list(
                    experience.activated_memory_refs
                ),
                mood_at_event_start=current_mood,
            )
        except (TypeError, ValueError):
            continue

        new_memory_impressions.append({
            "candidate_key": impression["candidate_key"],
            **computed,
        })

    return {
        "mood": mood_effect,
        "existing_memory_updates": existing_memory_updates,
        "new_memory_impressions": new_memory_impressions,
    }




def fallback_experience_appraisal(
    experience: ExperienceSlice,
    reason: str,
) -> ExperienceAppraisal:
    """
    整次 LLM 调用失败时的保守结果。

    只保留已经发生的事实：
    - mood 不变化；
    - 已有认知情绪不变化；
    - 不产生记忆候选。
    """
    review = fallback_experience_review(
        experience,
        reason=reason,
    )

    return ExperienceAppraisal(
        experience_review=review.to_dict(),
        emotion_assessment=fallback_emotion_assessment(reason),
        memory_assessment=fallback_memory_assessment(reason),
        fallback_regions=(
            "experience_review",
            "emotion_assessment",
            "memory_assessment",
        ),
    )

def _parse_json_text(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)

VALID_RELEVANCE = {"none", "low", "medium", "high"}
VALID_DIRECTIONS = {
    "strengthened",
    "slightly_positive",
    "unchanged",
    "slightly_negative",
    "weakened",
}

VALID_EVIDENCE_SOURCE_TYPES = {
    "perception",
    "agent_action",
    "observation",
    "memory",
}


def _clean_text(value: Any, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def _clean_text_list(value: Any, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []

    result = []
    for item in value:
        text = _clean_text(item)
        if text and text not in result:
            result.append(text)

    return result[:limit]


def _clean_emotion_evidence(
    value: Any,
    experience: ExperienceSlice,
) -> list[dict[str, str]]:
    """只保留能指向本次 ExperienceSlice 已知来源的证据。"""
    if not isinstance(value, list):
        return []

    valid_ids = {
        "perception": {
            experience.perception_event.event_id,
        },
        "agent_action": {
            action.action_id
            for action in experience.response_or_actions
        },
        "observation": {
            observation.event_id
            for observation in experience.observations
        },
        "memory": {
            str(ref.get("concept_id", "")).strip()
            for ref in experience.activated_memory_refs
            if ref.get("concept_id")
        },
    }

    result = []
    seen = set()

    for item in value:
        if not isinstance(item, dict):
            continue

        source_type = _clean_text(
            item.get("source_type"),
            limit=40,
        ).lower()
        source_id = _clean_text(
            item.get("source_id"),
            limit=120,
        )
        meaning = _clean_text(
            item.get("meaning"),
            limit=300,
        )

        if source_type not in VALID_EVIDENCE_SOURCE_TYPES:
            continue
        if source_id not in valid_ids[source_type]:
            continue
        if not meaning:
            continue

        dedupe_key = (source_type, source_id, meaning)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        result.append({
            "source_type": source_type,
            "source_id": source_id,
            "meaning": meaning,
        })

    return result[:8]


def _clean_emotion_assessment(
    raw: dict[str, Any],
    experience: ExperienceSlice,
) -> dict[str, Any]:
    relevance = _clean_text(
        raw.get("event_relevance")
    ).lower()
    valence = _clean_text(
        raw.get("event_valence")
    ).lower()
    salience = _clean_text(
        raw.get("salience")
    ).lower()

    if relevance not in VALID_RELEVANCE:
        raise ValueError("event_relevance 非法")
    if valence not in MOOD_EVENT_VALENCE_BIAS:
        raise ValueError("event_valence 非法")
    if salience not in MOOD_SALIENCE_FACTOR:
        raise ValueError("salience 非法")

    refs_by_id = {
        str(ref.get("concept_id", "")).strip(): ref
        for ref in experience.activated_memory_refs
        if ref.get("concept_id")
    }
    valid_ids = set(refs_by_id)
    evidence = _clean_emotion_evidence(
        raw.get("evidence"),
        experience,
    )

    if relevance == "high" and not evidence:
        raise ValueError("event_relevance=high 缺少可定位证据")

    affected = []
    seen_ids = set()

    if(
       relevance not in {"none", "low"}
       and salience in {"medium", "high"}
    ):
        items = raw.get("affected_memories", [])

        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue

                concept_id = _clean_text(
                    item.get("concept_id"),
                    limit=100,
                )
                direction = _clean_text(
                    item.get("change_direction")
                ).lower()
                strength = _clean_text(
                    item.get("strength")
                ).lower()

                if concept_id not in valid_ids:
                    continue
                if concept_id in seen_ids:
                    continue
                if direction not in VALID_DIRECTIONS:
                    continue
                if strength not in STRENGTH_SCORE_RANGES:
                    continue

                seen_ids.add(concept_id)
                affected.append({
                    "concept_id": concept_id,
                    "change_direction": direction,
                    "strength": strength,
                    "label_update": _clean_label_update(
                        item.get("label_update"),
                        forbidden_texts=(
                            refs_by_id[concept_id].get(
                                "canonical_name",
                                "",
                            ),
                            refs_by_id[concept_id].get("summary", ""),
                        ),
                    ),
                })

    # 无关或低相关事件不能悄悄影响 mood 和已有认知。
    if relevance in {"none", "low"}:
        valence = "neutral"
        affected = []
    # 低显著性事件仍可产生很小的 mood 波动，
    # 但不足以改变相对稳定的长期认知情绪印记
    elif salience == "low":
        affected = []

    return {
        "event_relevance": relevance,
        "event_valence": valence,
        "salience": salience,
        "reason": _clean_text(raw.get("reason")),
        "evidence": evidence,
        "affected_memories": affected,
        "uncertainties": _clean_text_list(
            raw.get("uncertainties")
        ),
    }

VALID_MEMORY_OPERATIONS = {
    "create",
    "update",
    "reinforce",
}

VALID_UPDATE_KINDS = {
    "replace",
    "extend",
    "correct",
    "contextualize",
}


def _clean_summary_update(value: Any) -> dict[str, Any] | None:
    """清洗变化意图；最终摘要仍必须明确保留来源边界。"""
    # 用字典表达完整的更新意图，而不是接受一段无法审查的纯字符串。
    # revised_summary 必须是整合后的完整摘要，不能只把新半句话追加到旧摘要后面。
    if not isinstance(value, dict):
        return None

    update_kind = _clean_text(value.get("update_kind"), limit=30).lower()
    new_information = _clean_text(value.get("new_information"), limit=600)
    revised_summary = _clean_text(value.get("revised_summary"), limit=900)
    if (
        update_kind not in VALID_UPDATE_KINDS
        or not new_information
        or not revised_summary
    ):
        return None

    return {
        "update_kind": update_kind,
        "new_information": new_information,
        "superseded_information": _clean_text(
            value.get("superseded_information"),
            limit=600,
        ),
        "revised_summary": revised_summary,
    }


def _clean_memory_candidates(
    value: Any,
    experience: ExperienceSlice,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    valid_ids = {
        str(ref.get("concept_id", "")).strip()
        for ref in experience.activated_memory_refs
        if ref.get("concept_id")
    }
    # 版本号由代码从本轮真实召回快照填写，不让模型自己“报告”版本。
    # 这样提交阶段才能判断：模型评价时读到的内容是否已经过期。
    revisions_by_id = {
        str(ref.get("concept_id", "")).strip(): max(
            1,
            int(ref.get("revision", 1)),
        )
        for ref in experience.activated_memory_refs
        if ref.get("concept_id")
    }

    candidates = []
    seen_keys = set()

    for item in value:
        if not isinstance(item, dict):
            continue

        candidate_key = _clean_text(
            item.get("candidate_key"),
            limit=100,
        )
        if not candidate_key:
            continue
        if candidate_key in seen_keys:
            continue

        # 重复 key 永远取模型输出中的第一个。
        seen_keys.add(candidate_key)

        operation = _clean_text(
            item.get("operation")
        ).lower()
        if operation not in VALID_MEMORY_OPERATIONS:
            continue

        target_concept_id = _clean_text(
            item.get("target_concept_id"),
            limit=100,
        ) or None

        identity_signature = item.get("identity_signature")
        if not isinstance(identity_signature, dict):
            identity_signature = {}

        memory_type = _clean_text(
            item.get("memory_type")
        ).lower()
        if memory_type not in ALLOWED_MEMORY_TYPES:
            memory_type = "other"

        concept_name = _clean_text(
            item.get("concept_name"),
            limit=160,
        )
        summary = _clean_text(
            item.get("summary"),
            limit=600,
        )

        if operation == "create":
            if not concept_name or not summary:
                continue
            if not identity_signature:
                continue
            target_concept_id = None
            base_revision = None
            summary_update = None
        else:
            # 已有认知操作只能引用本轮检索白名单。
            if target_concept_id not in valid_ids:
                continue
            # update 必须带着读取时的版本号一起往下游走。
            base_revision = revisions_by_id[target_concept_id]
            summary_update = _clean_summary_update(
                item.get("summary_update")
            )
            if operation == "update" and summary_update is None:
                continue
            if operation == "reinforce":
                summary_update = None

        candidates.append({
            "candidate_key": candidate_key,
            "operation": operation,
            "target_concept_id": target_concept_id,
            # 版本只来自真实召回快照，不接受 LLM 自报。
            "base_revision": base_revision,
            "concept_name": concept_name,
            "memory_type": memory_type,
            "identity_signature": deepcopy(identity_signature),
            "summary": summary,
            "summary_update": summary_update,
            "aliases_add": _clean_text_list(
                item.get("aliases_add"),
                limit=10,
            ),
            "canonical_name_update": _clean_text(
                item.get("canonical_name_update"),
                limit=160,
            ),
            "tags": _clean_text_list(
                item.get("tags"),
                limit=10,
            ),
            "source": _clean_text(
                item.get("source"),
                limit=50,
            ),
            "reason": _clean_text(item.get("reason")),
        })

    return candidates


def _clean_memory_assessment(
    raw: dict[str, Any],
    experience: ExperienceSlice,
) -> dict[str, Any]:
    candidates = _clean_memory_candidates(
        raw.get("memory_candidates"),
        experience,
    )

    impressions = _clean_new_memory_impressions(
        raw.get("new_memory_impressions"),
        candidates,
        experience,
    )

    return {
        "memory_candidates": candidates,
        "new_memory_impressions": impressions,
        "reason": _clean_text(raw.get("reason")),
        "uncertainties": _clean_text_list(
            raw.get("uncertainties")
        ),
    }

VALID_PERSONA_EFFECTS = {
    "fitting",
    "neutral",
    "conflicting",
}


def _clean_new_memory_impressions(
    value: Any,
    candidates: list[dict[str, Any]],
    experience: ExperienceSlice,
) -> list[dict[str, Any]]:
    create_keys = [
        item["candidate_key"]
        for item in candidates
        if item["operation"] == "create"
    ]
    create_key_set = set(create_keys)
    candidates_by_key = {
        item["candidate_key"]: item
        for item in candidates
        if item["operation"] == "create"
    }

    valid_concept_ids = {
        str(ref.get("concept_id", "")).strip()
            for ref in experience.activated_memory_refs
        if ref.get("concept_id")
    }

    cleaned_by_key = {}
    seen_keys = set()

    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue

            candidate_key = _clean_text(
                item.get("candidate_key"),
                limit=100,
            )
            if not candidate_key:
                continue
            if candidate_key in seen_keys:
                continue

            # 重复 key 只处理第一个，即使第一个后来被判非法。
            seen_keys.add(candidate_key)

            if candidate_key not in create_key_set:
                continue

            persona_effect = _clean_text(
                item.get("persona_effect")
            ).lower()
            if persona_effect not in VALID_PERSONA_EFFECTS:
                persona_effect = "neutral"

            candidate_valence = _clean_text(
                item.get("candidate_valence")
            ).lower()

            # 单个新候选直接复用全局 event_valence，
            # 不允许重复进行一次同构判断。
            if len(create_keys) <= 1:
                candidate_valence = None
            elif candidate_valence not in MOOD_EVENT_VALENCE_BIAS:
                candidate_valence = None

            related_ids = []
            raw_ids = item.get(
                "direct_related_concept_ids",
                [],
            )
            if isinstance(raw_ids, list):
                for concept_id in raw_ids:
                    concept_id = _clean_text(
                        concept_id,
                        limit=100,
                    )
                    if (
                        concept_id in valid_concept_ids
                        and concept_id not in related_ids
                    ):
                        related_ids.append(concept_id)

            cleaned_by_key[candidate_key] = {
                "candidate_key": candidate_key,
                "candidate_valence": candidate_valence,
                "persona_effect": persona_effect,
                "direct_related_concept_ids": related_ids,
                "label_update": _clean_label_update(
                    item.get("label_update"),
                    forbidden_texts=(
                        candidates_by_key[candidate_key].get(
                            "concept_name",
                            "",
                        ),
                        candidates_by_key[candidate_key].get("summary", ""),
                    ),
                ),
                "fallback_to_neutral": False,
            }

    result = []

    # 按事实候选顺序输出，避免 LLM 输出顺序改变关联。
    for candidate_key in create_keys:
        impression = cleaned_by_key.get(candidate_key)

        if impression is None:
            impression = {
                "candidate_key": candidate_key,
                "candidate_valence": None,
                "persona_effect": "neutral",
                "direct_related_concept_ids": [],
                "label_update":{
                    "label": "中性",
                    "polarity": "neutral",
                    "strength": "neutral",
                } ,
                "fallback_to_neutral": True,
            }

        result.append(impression)

    return result

VALID_DOWNSTREAM_USES = {
    "emotion",
    "memory",
}


def _clean_experience_review(
    raw: dict[str, Any],
) -> dict[str, Any]:
    summary = _clean_text(
        raw.get("experience_summary"),
        limit=600,
    )
    if not summary:
        raise ValueError("experience_summary 缺失")

    points = []
    raw_points = raw.get("salient_points", [])

    if isinstance(raw_points, list):
        for item in raw_points:
            if not isinstance(item, dict):
                continue

            point = _clean_text(
                item.get("point"),
                limit=300,
            )
            evidence = _clean_text(
                item.get("evidence"),
                limit=400,
            )

            # 没有证据的显著点不能进入后续情绪或记忆判断。
            if not point or not evidence:
                continue

            downstream = [
                value
                for value in _clean_text_list(
                    item.get("possible_downstream_use"),
                    limit=4,
                )
                if value in VALID_DOWNSTREAM_USES
            ]

            points.append({
                "point": point,
                "evidence": evidence,
                "why_it_matters": _clean_text(
                    item.get("why_it_matters"),
                    limit=300,
                ),
                "possible_downstream_use": downstream,
            })

    return {
        "experience_summary": summary,
        "situated_interpretation": (
            _clean_text(
                raw.get("situated_interpretation"),
                limit=600,
            )
            or "本轮没有形成额外的情境解释。"
        ),
        "salient_points": points[:8],
        "uncertainties": _clean_text_list(
            raw.get("uncertainties"),
            limit=8,
        ),
        "do_not_assume": _clean_text_list(
            raw.get("do_not_assume"),
            limit=8,
        ),
    }

VALID_LABEL_POLARITIES = {
    "positive",
    "neutral",
    "negative",
}

VALID_LABEL_STRENGTHS = {
    "neutral",
    "slight",
    "moderate",
    "strong",
}


def _normalize_label_comparison(value: Any) -> str:
    """只用于识别完全复制；不尝试做中文语义相似度。"""
    text = str(value or "").strip().lower()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _clean_label_update(
    value: Any,
    *,
    forbidden_texts: tuple[Any, ...] = (),
) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None

    label = _clean_text(
        value.get("label"),
        limit=80,
    )
    polarity = _clean_text(
        value.get("polarity")
    ).lower()
    strength = _clean_text(
        value.get("strength")
    ).lower()

    if not label:
        return None
    if polarity not in VALID_LABEL_POLARITIES:
        return None
    if strength not in VALID_LABEL_STRENGTHS:
        return None

    if polarity == "neutral" and strength != "neutral":
        return None
    if polarity != "neutral" and strength == "neutral":
        return None

    normalized_label = _normalize_label_comparison(label)
    if any(
        normalized_label
        and normalized_label == _normalize_label_comparison(text)
        for text in forbidden_texts
        if text
    ):
        return None

    return {
        "label": label,
        "polarity": polarity,
        "strength": strength,
    }



def _build_appraisal_prompt(
    experience: ExperienceSlice,
    persona_context: dict[str, Any],
) -> str:
    """构造一次 ExperienceAppraisal 的完整输出协议。"""
    output_contract = {
        "experience_review": {
            "experience_summary": "本轮实际发生了什么",
            "situated_interpretation": "结合上下文后的保守解释",
            "salient_points": [],
            "uncertainties": [],
            "do_not_assume": [],
        },
        "emotion_assessment": {
            "event_relevance": "none | low | medium | high",
            "event_valence": (
                "strong_positive | mild_positive | neutral | "
                "mild_negative | strong_negative"
            ),
            "salience": "low | medium | high",
            "reason": "简短理由",
            "evidence": [],
            "affected_memories": [],
            "uncertainties": [],
        },
        "memory_assessment": {
            "memory_candidates": [],
            "new_memory_impressions": [],
            "reason": "记忆候选判断摘要",
            "uncertainties": [],
        },
    }

    item_contracts = {
        "salient_point": {
            "point": "对后续评价真正重要的观察或解释",
            "evidence": "可在 ExperienceSlice 中定位的直接证据",
            "why_it_matters": "它为什么可能影响情绪或记忆",
            "possible_downstream_use": ["emotion", "memory"],
        },
        "emotion_evidence": {
            "source_type": (
                "perception | agent_action | observation | memory"
            ),
            "source_id": "对应的 event_id、action_id 或 concept_id",
            "meaning": "该来源直接支持什么结论",
        },
        "affected_memory": {
            "concept_id": "本轮 activated_memory_refs 中的 concept_id",
            "change_direction": (
                "strengthened | slightly_positive | unchanged | "
                "slightly_negative | weakened"
            ),
            "strength": "slight | moderate | strong",
            "label_update": {
                "label": "简短、符合 persona 的主观印象标签",
                "polarity": "positive | neutral | negative",
                "strength": "neutral | slight | moderate | strong",
            },
        },
        "memory_candidate": {
            "candidate_key": "本次输出内唯一，例如 candidate_1",
            "operation": "create | update | reinforce",
            "target_concept_id": None,
            "concept_name": "原子认知名称",
            "memory_type": (
                "entity | preference | relationship | "
                "interaction_pattern | event | knowledge | other"
            ),
            "identity_signature": {
                "subject": "稳定主体",
                "relation": "归一化关系",
                "object": "核心对象",
                "qualifier": None,
            },
            "summary": "保留来源边界的原子认知摘要",
            "summary_update": {
                "update_kind": "replace | extend | correct | contextualize",
                "new_information": "本轮新增、替代或纠正的信息",
                "superseded_information": "被明确覆盖的信息；没有则空字符串",
                "revised_summary": "旧认知与新证据整合后的完整当前摘要",
            },
            "aliases_add": [],
            "canonical_name_update": "",
            "tags": [],
            "source": "user_told | inferred | system",
            "reason": "满足哪项长期价值标准",
        },
        "new_memory_impression": {
            "candidate_key": "对应 create 候选的 candidate_key",
            "candidate_valence": None,
            "persona_effect": "fitting | neutral | conflicting",
            "direct_related_concept_ids": [],
            "label_update": {
                "label": "简短、符合 persona 的初始印象标签",
                "polarity": "positive | neutral | negative",
                "strength": "neutral | slight | moderate | strong",
            },
        },
    }

    scoring_reference = {
        "existing_memory": {
            "formula": "clamp(old_score + delta, 0, 100)",
            "direction_delta": EMOTION_AFFECT_DIRECTION_DELTA,
        },
        "new_memory": {
            "base": EMOTION_SCORE_INITIAL_BASE,
            "event_bias": EVENT_INITIAL_BIAS,
            "persona_bias": PERSONA_INITIAL_BIAS,
            "memory_bias": (
                "clamp((direct_related_score - 50) * "
                f"{MEMORY_INITIAL_BIAS_FACTOR}, -10, 10)"
            ),
            "mood_bias": (
                f"clamp((mood_at_event_start - {MOOD_BASELINE}) * "
                f"{MOOD_INITIAL_BIAS_FACTOR}, -5, 5)"
            ),
            "final_range": [
                EMOTION_SCORE_INITIAL_MIN,
                EMOTION_SCORE_INITIAL_MAX,
            ],
        },
        "label_strength": {
            "neutral": "score == 50",
            "slight": "0 < abs(score - 50) <= 10",
            "moderate": "10 < abs(score - 50) <= 25",
            "strong": "abs(score - 50) > 25",
        },
    }

    return f"""
你是通用类人 Agent 框架中的 ExperienceAppraisal（经验即时评价）模块。
你的输入是一段已经完成的 ExperienceSlice，而不是固定的一轮聊天。
感知来源可能是用户文本、工具结果、视觉或听觉描述、环境观察、
Agent 动作反馈、系统状态变化或后台事件。

你只负责三件事：
1. 忠实回看实际发生的经验，并区分事实、解释和不确定性。
2. 从当前 persona 的视角提出情绪语义评价。
3. 提出原子记忆候选和新认知初始印象建议。

你不生成对外回复，不规划下一步行动，不调用工具，不写数据库，
不修改 mood/energy，不决定最终认知身份，也不输出分析过程或思维链。

你必须只输出一个 JSON object，并且只能包含：
experience_review、emotion_assessment、memory_assessment。

【必须按顺序完成的评价流程】

第一步：证据重建
- 分别查看 perception_event、response_or_actions、observations。
- perception_understanding 和 preceding_context 只是带来源的上下文，
  不能覆盖原始感知；activated_memory_refs 是本轮自然浮现的长期认知，
  它不是穷尽查询结果，也不是本轮新发生的事实。
- capability_snapshot 与 memory_activation_state 是框架记录的当时运行真值。
  AgentAction 可以证明 Agent 实际表达或执行了什么；Agent 对自身主观体验的
  表达可以作为第一人称证据，但关于“想起、查过、保存、执行”等认知访问、
  工具或副作用的自述，只有与上述结构化状态或 observations 一致时才成立。
- activated_memory_refs 为空只表示本轮没有认知自然浮现，不能推出长期认知
  不存在，也不能据此反推 Agent 以前有来源的回答是编造。
- experience_summary 只写实际发生的内容。
- situated_interpretation 可以解释情境，但必须保守；证据不足时写入
  uncertainties/do_not_assume，不能把推测升级成事实。
- Persona 只能影响角色如何解释和感受，不能成为本轮新事件。
- 训练知识不能伪装成角色亲身经历。
- persona 的 allowed_domains 只约束无来源的参数知识，不是记忆候选白名单。
  当前感知、用户告知、工具结果或观察明确提供的领域外内容仍是有效事件证据，
  不能仅因主题在允许领域外就降低显著性或拒绝形成带来源的认知候选。

第二步：通用情境刻画
- 不要把事件强行塞进聊天专用的 event_type 枚举，也不要输出 event_type。
- 在内部同时检查这些非互斥维度：来源与模态、谁是行动者、影响对象、
  是否新颖、是否符合预期、是否涉及目标/关系/边界/自主性/安全、
  是否可控或可应对、是否已有可靠上下文。
- 事件可以同时包含“事件结果、他人行为、对象吸引或排斥”等多个侧面；
  不要强迫它只能属于其中一类。

第三步：相关性、效价与显著性
- event_relevance 衡量事件是否真正关系到当前主体，而不是事件是否被提及：
  none=无实际关联；low=只有表面或例行关联；medium=有明确但有限的影响；
  high=直接影响核心目标、重要关系、边界、自主性、安全或关键任务。
- high 必须点名至少一项受到重大影响的具体利益，并在 evidence 中给出
  可定位的直接来源。不能只因为事件“可能”涉及亲密、拒绝或冲突就判 high。
  如果重大影响本身已经确认，只是原因未知，仍可判 high。
- event_valence 是从当前 persona 的目标、价值、边界、期望和已有关系经验
  出发，对整段 ExperienceSlice 的统一方向判断。
- 一段 ExperienceSlice 可能同时包含知识请求、任务内容和直接关系/边界行为。
  不要用较中性的任务部分冲淡更直接影响主体的部分；event_relevance、salience
  和 event_valence 应由对主体最具实际影响、且有证据的部分主导。
- 用户喜欢某物不等于 Agent 喜欢它；礼貌、感谢、请求、拒绝、沉默、
  负面词汇也都不自动等于某个效价。当前 mood 可以影响体验背景，
  但不能单独制造事实或情绪方向。
- salience 衡量这段经验对后续状态或长期理解的影响程度：
  low=例行、短暂、可逆且缺乏长期后果；medium=有新信息或有限后果；
  high=明确而重大的关系、目标、边界、安全或持续性变化。
- event_relevance 为 none/low 时，event_valence 必须为 neutral，
  affected_memories 必须为空。
- salience 为 low 时，affected_memories 必须为空；事件仍可保留有证据的
  event_valence，供代码计算很小的 mood 候选。
- event_valence 是全局唯一效价，不得输出 event_effect。

第四步：已有认知情绪印记
- 只有 relevance 与 salience 都是 medium/high 时才检查 affected_memories。
- 逐条检查 activated_memory_refs，但自然浮现不等于受影响，提及也不等于改变。
- 只有本轮直接改变了主体对某条认知的感受时才输出；必须在
  emotion_assessment.evidence 中给出可定位的直接来源。
- concept_id 只能来自 activated_memory_refs；不能根据名称猜 id。
- change_direction 表示分数向上或向下移动，精确数值见“代码数值参考”。
- 外层 strength 只描述本轮变化幅度；label_update.strength 描述最终分数
  距离 50 的强度，二者不能混用。
- label_update 是建议。无法可靠提出时可以为 null；不能为了保住标签而
  改变 polarity/strength，最终仍由代码校验。
- label 的隐含主体必须是当前 Agent，只写 Agent 对该认知的主观态度，
  例如“略感亲近”“保持戒备”“平静接纳”。认知对象已经由 concept_id 关联，
  不要把 canonical_name、用户事实或摘要换一种说法当作情绪标签。

第五步：长期记忆候选
- 先问：这条内容在未来跨事件理解、关系连续性、任务延续或个体一致性中
  是否仍有价值？没有长期价值就不要记录。
- 值得候选化的内容通常包括：明确且相对稳定的个人事实/偏好/承诺；
  可复用的实体与关系；需要延续的任务或重要事件；有充分证据的关系变化；
  明确的认知修正或重复确认。
- 由当前感知、用户告知、工具结果或观察提供的领域外事实，只要有明确来源且未来
  可能再次用于理解、回应或任务延续，也可以成为 entity/knowledge/event 候选。
  领域外不能调用参数知识，不等于领域外内容不能被学习或形成长期认知。
- 用户或其他来源主动提供一段围绕命名实体的可复用资料，并且当前或后续互动正在
  依赖这段资料时，应至少提取有长期价值的实体/知识候选；保留来源并压缩摘要，
  不要把整段原文原样塞进记忆，也不要补充来源之外的事实。
- 普通寒暄、一次性措辞、无后续价值的瞬时状态、仅因自然浮现的旧认知，
  都不应生成候选。AgentAction 可以证明 Agent 实际表达或执行了什么，也可以
  作为其主观反应的一手证据；但不能单独证明用户事实、数据库状态、工具结果
  或其他 Agent 当时无权观察的系统事实。
- 单次行为通常不足以建立 interaction_pattern；除非输入明确表达一般规律，
  否则最多记录为 event 候选，等待未来多次证据形成模式。
- 直接针对 Agent 或关系对象的亲密、拒绝、冲突、边界、许可或自主性相关行为，
  即使只发生一次，也可能作为 event 候选具有关系连续性价值。文本或模拟环境中的
  动作仍是一次有来源的互动事件；summary 必须说明它是文字描述、模拟动作或真实
  观察，不能把它伪装成现实物理接触。
- 一次感谢或顺从通常不足以更新 relationship；必须有明确的信任、承诺、
  边界、角色关系或持续互动预期变化。
- 用户明确表示“不记住”或“仅作临时信息”时，不为对应内容生成长期记忆候选。
- 事实与感受分离：memory_candidates 描述“发生/得知了什么”，
  new_memory_impressions 描述“当前主体对此形成什么初始感受”。

第六步：候选操作与身份边界
- 每条候选只能表达一个原子命题；喜欢 A 但不喜欢 B 必须拆成两条。
- create 表示“提交一个可能的新身份候选”，不是最终宣判数据库中不存在；
  最终 same/related/new 由后续 resolver/judge 决定。
- create 必须有唯一 candidate_key、concept_name、非空 identity_signature、
  summary；target_concept_id 必须为 null。
- update 只在本轮明确修正或扩展了已召回的同一认知时使用；reinforce 只在
  本轮再次确认同一事实且无需改写事实内容时使用。
- update/reinforce 的 target_concept_id 只能来自 activated_memory_refs。
- update 必须填写结构化 summary_update：update_kind 只能是 replace、extend、
  correct、contextualize；new_information 写本轮确认的变化；只有旧信息被明确
  覆盖时才写 superseded_information；revised_summary 必须是保留来源边界的完整
  当前整合摘要。不能只输出新增半句话，也不能把新旧摘要机械拼接。
- reinforce 的 summary_update 必须为 null；create 只使用 summary，summary_update
  必须为 null。base_revision 由代码从召回快照补入，不要输出。
- 不允许输出 affect_only；已有认知的情绪变化只走 affected_memories。
- summary 必须保留认识来源，例如“用户表示……”“工具结果显示……”，
  不得把 user_told 或 inferred 写成无来源的客观世界真理。
- 用户修正旧陈述时，只覆盖被明确否定或替换的原子命题；同一句旧陈述中
  没有被撤回的其他事实保持不变，不能扩大作废范围。

第七步：新认知初始印象
- new_memory_impressions 只对应 operation=create，并通过 candidate_key 关联。
- 每个 create 候选都应有一条印象；若证据不足，仍应诚实给出 neutral 或
  让 label_update 为 null，而不是省略后让身份关联丢失。
- persona_effect 默认是 neutral。只有候选内容本身直接触碰 persona 明确声明的
  目标、价值、边界或关系期待，并能说明方向时，才输出 fitting/conflicting。
  “知道这件事有助于服务用户”“这条事实以后可能有用”不等于 fitting，
  普通个人事实通常是 neutral。它不是重复判断事件好坏。
- direct_related_concept_ids 只能引用与该候选直接相关且本轮自然浮现的认知；
  主题相似、同一主体或弱联想都不算直接相关。
- 只有同一 ExperienceSlice 产生多个 create 候选，且它们确有不同效价时，
  才填写 candidate_valence；否则必须为 null 并复用 event_valence。
- 不得输出 emotion_score；可参考“代码数值参考”估计 label_update 的
  polarity/strength，最终分数由代码计算。
- 新认知 label 同样只描述 Agent 的主观态度，不能复述 concept_name 或 summary；
  没有可靠、独特的主观态度时输出 null。

第八步：一致性复核
- 三个顶层区域必须都存在；没有项目时使用空数组。
- 不得复制字段协议中的占位内容，不得发明 concept_id，不得重复 candidate_key。
- 所有 label_update 必须是 null，或同时包含 label/polarity/strength。
- 不得混入 event_type、event_effect、initial_valence、bonding_intensity、
  confidence、affect_only、emotion_score 或数据库写入决定。
- 只输出最终 JSON，不要 Markdown 代码块，不要额外解释。

【校准反例】

1. 普通问候、礼貌感谢：不能仅凭礼貌词自动判为正向或关系升级；应结合
   persona、已有关系和实际后果，很多情况下是 neutral/low。
2. 感知到“用户喜欢咖啡”：这是用户偏好事实候选；它不自动说明 Agent
   喜欢咖啡，也不自动要求已有认知情绪变化。
3. 工具返回任务失败：来源是工具/动作反馈，可对当前任务产生负向评价；
   除非结果同时提供用户事实，否则不要创建“关于用户”的记忆。
4. 视觉中第一次看到某人挥手：这是一次观察或事件，不足以单独建立
   “此人总用挥手表达亲近”的 interaction_pattern。
5. 用户提供“喜欢苹果”这类普通个人事实：它可能值得成为事实候选，但
   persona_effect 通常是 neutral；不能因为记住它有助于服务就输出 fitting。
6. 用户说“我刚才说错了，我不喜欢橘子，我喜欢苹果”：只修正橘子相关
   原子事实，不能撤回同一句旧陈述里没有被提及的其他偏好。
7. 用户提供一段关于领域外命名实体的资料并继续要求基于它讨论：可以创建带
   user_told/source 限定的 entity 或 knowledge 候选；不能以“领域外”为由丢弃，
   也不能加入资料中没有的背景知识。
8. 感知到某个参与者直接对 Agent 作出涉及亲密或边界的动作：不能因为动作是
   文本模拟就当作没发生；应评价这次互动对关系、边界和情绪的意义。单次通常记录
   为 event，而不是直接宣判稳定 interaction_pattern 或 relationship 已改变。

【代码数值参考】

{json.dumps(scoring_reference, ensure_ascii=False, indent=2)}

【ExperienceSlice】

{json.dumps(experience.to_dict(), ensure_ascii=False, indent=2)}

【独立 Persona 快照】

{json.dumps(persona_context, ensure_ascii=False, indent=2)}

【唯一允许的顶层输出结构】

{json.dumps(output_contract, ensure_ascii=False, indent=2)}

【数组子项字段协议】

以下对象只说明字段，不是要复制进数组的示例。没有充分证据时数组必须为空。

{json.dumps(item_contracts, ensure_ascii=False, indent=2)}
""".strip()


def appraise_experience(
    experience: ExperienceSlice,
    persona_context: dict[str, Any],
    llm,
) -> ExperienceAppraisal:
    """
    用一次 LLM 调用评价一个 ExperienceSlice。

    三个区域分别清洗和回退；本函数只生成结构化评价，
    不修改运行状态，也不写入长期记忆。
    """
    prompt = _build_appraisal_prompt(
        experience,
        persona_context,
    )

    try:
        json_llm = llm.bind(
            response_format={"type": "json_object"}
        )
        response = json_llm.invoke(prompt)
        raw = _parse_json_text(response.content)
    except Exception:
        return fallback_experience_appraisal(
            experience,
            "ExperienceAppraisal LLM 调用或解析失败",
        )

    if not isinstance(raw, dict):
        return fallback_experience_appraisal(
            experience,
            "ExperienceAppraisal 顶层不是 JSON object",
        )

    fallback_regions = []

    review = raw.get("experience_review")
    if not isinstance(review, dict):
        review = fallback_experience_review(
            experience,
            "experience_review 区域非法",
        ).to_dict()
        fallback_regions.append("experience_review")
    else:
        try:
            review = _clean_experience_review(review)
        except ValueError as exc:
             review = fallback_experience_review(
                 experience,
                 str(exc),
             ).to_dict()
             fallback_regions.append("experience_review")

    emotion = raw.get("emotion_assessment")
    if not isinstance(emotion, dict):
        emotion = fallback_emotion_assessment(
            "emotion_assessment 区域非法"
        )
        fallback_regions.append("emotion_assessment")
    else:
        try:
            emotion = _clean_emotion_assessment(
                emotion,
                experience,
            )
        except ValueError as exc:
            emotion = fallback_emotion_assessment(str(exc))
            fallback_regions.append("emotion_assessment")

    memory = raw.get("memory_assessment")
    if not isinstance(memory, dict):
        memory = fallback_memory_assessment(
            "memory_assessment 区域非法"
        )
        fallback_regions.append("memory_assessment")
    else:
        memory = _clean_memory_assessment(
            memory,
            experience,
        )

    return ExperienceAppraisal(
        experience_review=review,
        emotion_assessment=emotion,
        memory_assessment=memory,
        fallback_regions=tuple(fallback_regions),
    )
