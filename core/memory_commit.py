"""Step 6 的单事件记忆与情绪提交。

本模块属于框架层提交逻辑：输入已经由 Step 5 清洗过的 appraisal/effects，
不重新让 LLM 理解事件。身份解析仍由 resolver/judge 负责，数据库写入由
本模块按固定顺序编排。所有调用发生在 CommitWorker 的单一线程中，因此
同一时刻不会有两个事件同时覆盖同一条 MemoryEntity。
"""

import hashlib
from copy import deepcopy
from typing import Any

from core.commit_worker import CommitTask
from emotion.appraisal_rules import compute_existing_memory_emotion_update
from emotion.manager import commit_mood_effect
from memory.identity_service import resolve_identity_with_judge
from memory.relation_service import build_relation_for_candidate
from memory.revision_resolver import resolve_memory_revision
from memory.schema import MemoryEntity
from memory.store_manager import (
    read_entity,
    reinforce_entity,
    update_entity_content,
    upsert_entity,
)
from memory.sql_store import upsert_relation


_VALENCE_TO_DIRECTION = {
    "strong_positive": "strengthened",
    "mild_positive": "slightly_positive",
    "neutral": "unchanged",
    "mild_negative": "slightly_negative",
    "strong_negative": "weakened",
}


def _stable_candidate_id(job_id: str, candidate_key: str) -> str:
    """同一 appraisal 候选在重复交付时始终得到同一个 concept_id。"""
    raw = f"{job_id}|{candidate_key}".encode("utf-8")
    return "cog_" + hashlib.sha256(raw).hexdigest()[:24]


def _appraisal_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {}


def _candidate_impression_map(effects: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("candidate_key")): item
        for item in effects.get("new_memory_impressions", [])
        if isinstance(item, dict) and item.get("candidate_key")
    }


def _build_entity(candidate: dict[str, Any], impression: dict[str, Any], concept_id: str) -> MemoryEntity:
    """把已经清洗的 create 候选转换为实体；不再进行语义判断。"""
    return MemoryEntity(
        concept_id=concept_id,
        canonical_name=str(candidate.get("concept_name") or "未命名认知").strip(),
        aliases=list(candidate.get("aliases_add") or []),
        memory_type=str(candidate.get("memory_type") or "other"),
        identity_signature=deepcopy(candidate.get("identity_signature") or {}),
        summary=str(candidate.get("summary") or "").strip(),
        tags=list(candidate.get("tags") or []),
        emotion_score=float(impression.get("emotion_score", 50.0)),
        emotion_label=str(impression.get("emotion_label") or "中性"),
        mention_count=1,
        source=str(candidate.get("source") or "inferred"),
        confidence=0.8,
    )


def _merge_fact_fields(entity: MemoryEntity, candidate: dict[str, Any]) -> None:
    """合并非摘要事实字段；不触碰情绪分数或标签。"""
    canonical_update = str(
        candidate.get("canonical_name_update") or ""
    ).strip()
    if canonical_update:
        entity.canonical_name = canonical_update
    for alias in candidate.get("aliases_add") or []:
        alias = str(alias).strip()
        if alias and alias not in entity.aliases:
            entity.aliases.append(alias)
    for tag in candidate.get("tags") or []:
        tag = str(tag).strip()
        if tag and tag not in entity.tags:
            entity.tags.append(tag)


def _apply_revision(
    entity: MemoryEntity,
    candidate: dict[str, Any],
    revision: dict[str, Any],
) -> dict[str, Any]:
    """应用已裁决的内容关系；冲突和不确定性不会覆盖当前认知。"""
    # resolver 只负责内容关系，真正的数据库写入仍由本模块完成。
    relation = revision.get("content_relation")
    if relation == "duplicate":
        saved = reinforce_entity(entity.concept_id)
        return {
            "status": "reinforced",
            "content_relation": relation,
            "concept_id": entity.concept_id,
            "mention_count": saved.mention_count if saved else None,
            "revision": saved.revision if saved else entity.revision,
        }
    # 证据冲突或判断不确定时保守保留当前事实，不能让一次模糊输入覆盖它。
    if relation in {"conflict", "uncertain"}:
        return {
            "status": "preserved",
            "content_relation": relation,
            "concept_id": entity.concept_id,
            "revision": entity.revision,
            "reason": revision.get("reason"),
        }

    revised_summary = str(revision.get("revised_summary") or "").strip()
    if not revised_summary:
        return {
            "status": "preserved",
            "content_relation": "uncertain",
            "concept_id": entity.concept_id,
            "revision": entity.revision,
            "reason": "缺少完整整合摘要",
        }

    entity.summary = revised_summary
    _merge_fact_fields(entity, candidate)
    entity.mention_count += 1
    saved = update_entity_content(
        entity,
        expected_revision=entity.revision,
    )
    if saved is None:
        return {
            "status": "preserved",
            "content_relation": "uncertain",
            "concept_id": entity.concept_id,
            "revision": entity.revision,
            "reason": "提交时版本再次变化，未覆盖最新认知",
        }
    return {
        "status": "updated",
        "content_relation": relation,
        "concept_id": saved.concept_id,
        "mention_count": saved.mention_count,
        "revision": saved.revision,
    }


def _apply_fact_update(
    candidate: dict[str, Any],
    *,
    event_evidence: dict[str, Any],
    revision_llm,
) -> dict[str, Any]:
    """提交 update/reinforce；版本过期时才调用内容修订裁决器。"""
    target_id = str(candidate.get("target_concept_id") or "").strip()
    entity = read_entity(target_id)
    if entity is None:
        return {"status": "skipped", "reason": "target_not_found", "target_concept_id": target_id}

    operation = candidate.get("operation")
    if operation == "reinforce":
        saved = reinforce_entity(target_id)
        return {"status": "reinforced", "concept_id": saved.concept_id if saved else target_id}

    summary_update = candidate.get("summary_update")
    if not isinstance(summary_update, dict):
        return {
            "status": "preserved",
            "reason": "invalid_summary_update",
            "concept_id": target_id,
        }

    # appraisal 生成候选时读取的是 base_revision；提交时再次比较，检查快照是否过期。
    base_revision = candidate.get("base_revision")
    if base_revision != entity.revision:
        # 版本过期时不能直接套用旧 summary，先基于最新实体重新判断新旧内容关系。
        revision = resolve_memory_revision(
            current_entity=entity,
            candidate=candidate,
            event_evidence=event_evidence,
            llm=revision_llm,
        )
        return _apply_revision(entity, candidate, revision)

    entity.summary = summary_update["revised_summary"]
    _merge_fact_fields(entity, candidate)
    entity.mention_count += 1
    # 数据库再次用 WHERE revision = expected_revision 做最后一道并发保护。
    saved = update_entity_content(
        entity,
        expected_revision=base_revision,
    )
    if saved is None:
        # 即使前面的比较通过，提交瞬间仍可能被别的写入抢先；因此要重读再裁决。
        latest = read_entity(target_id)
        if latest is None:
            return {
                "status": "skipped",
                "reason": "target_not_found_after_revision_change",
                "concept_id": target_id,
            }
        revision = resolve_memory_revision(
            current_entity=latest,
            candidate=candidate,
            event_evidence=event_evidence,
            llm=revision_llm,
        )
        return _apply_revision(latest, candidate, revision)
    return {
        "status": "updated",
        "content_relation": summary_update["update_kind"],
        "concept_id": saved.concept_id,
        "mention_count": saved.mention_count,
        "revision": saved.revision,
    }


def _apply_existing_emotion_update(update: dict[str, Any]) -> dict[str, Any]:
    """提交时重新读取当前分数，避免使用 appraisal 快照的过时绝对值。"""
    concept_id = str(update.get("concept_id") or "").strip()
    entity = read_entity(concept_id)
    if entity is None:
        return {"status": "skipped", "reason": "concept_not_found", "concept_id": concept_id}

    computed = compute_existing_memory_emotion_update(
        entity.emotion_score,
        update,
    )
    entity.emotion_score = computed["emotion_score"]
    entity.emotion_label = computed["emotion_label"]
    saved = upsert_entity(entity)
    return {
        "status": "updated",
        "concept_id": saved.concept_id,
        "emotion_score": saved.emotion_score,
        "emotion_label": saved.emotion_label,
        "score_delta": computed["score_delta"],
    }


class MemoryCommitService:
    """CommitWorker 使用的默认提交函数。"""

    def __init__(self, *, identity_llm) -> None:
        self._identity_llm = identity_llm

    def __call__(self, task: CommitTask) -> dict[str, Any]:
        appraisal_job = task.appraisal_job or {}
        if appraisal_job.get("status") != "completed":
            # appraisal 失败必须显式前进，不产生任何伪造记忆或情绪。
            return {
                "status": "skipped_appraisal_failure",
                "appraisal_status": appraisal_job.get("status"),
            }

        effects = appraisal_job.get("effects") or {}
        appraisal = _appraisal_dict(appraisal_job.get("appraisal"))
        emotion = appraisal.get("emotion_assessment") or {}
        memory = appraisal.get("memory_assessment") or {}
        event_evidence = appraisal_job.get("event_evidence") or {}

        result: dict[str, Any] = {
            "mood": None,
            "facts": [],
            "emotion_updates": [],
            "relations": [],
        }

        # 1. mood 只使用 Step 5 已经计算和钳制的 impact，不能再次调用旧的
        # apply_post_turn_update，否则会重复做边界衰减。
        mood = effects.get("mood") or {}
        # 不用 int(...) 强制转换。若上游意外传来 1.5、True 或越界值，
        # 情绪规则层必须显式拒绝，不能静默截断成另一个合法冲击。
        mood_impact = mood.get("mood_impact", 0)
        result["mood"] = commit_mood_effect(
            task.thread_id,
            mood_impact=mood_impact,
        )

        candidates = [
            item for item in memory.get("memory_candidates", [])
            if isinstance(item, dict)
        ]
        impressions = _candidate_impression_map(effects)
        related_to_create: list[tuple[str, str, str, str]] = []
        same_emotion_updates: list[dict[str, Any]] = []

        # 2. 事实候选先经过 resolver/judge，再写实体；事实阶段不修改情绪。
        for candidate in candidates:
            operation = candidate.get("operation")
            if operation in {"update", "reinforce"}:
                result["facts"].append(_apply_fact_update(
                    candidate,
                    event_evidence=event_evidence,
                    revision_llm=self._identity_llm,
                ))
                continue
            if operation != "create":
                continue

            candidate_key = str(candidate.get("candidate_key") or "").strip()
            impression = impressions.get(candidate_key) or {
                "emotion_score": 50.0,
                "emotion_label": "中性",
                "fallback_to_neutral": True,
            }
            identity = resolve_identity_with_judge(
                candidate,
                self._identity_llm,
            )
            decision = identity.get("decision")
            target_id = identity.get("target_concept_id")

            if decision == "same" and target_id:
                current = read_entity(target_id)
                if current is None:
                    result["facts"].append({
                        "status": "skipped",
                        "reason": "same_target_not_found",
                        "concept_id": target_id,
                    })
                    continue
                revision = resolve_memory_revision(
                    current_entity=current,
                    candidate=candidate,
                    event_evidence=event_evidence,
                    llm=self._identity_llm,
                )
                fact_result = _apply_revision(
                    current,
                    candidate,
                    revision,
                )
                fact_result["identity_decision"] = "same"
                result["facts"].append(fact_result)
                valence = impression.get("candidate_valence") or emotion.get("event_valence")
                direction = _VALENCE_TO_DIRECTION.get(valence, "unchanged")
                same_emotion_updates.append({
                    "concept_id": target_id,
                    "change_direction": direction,
                    "label_update": impression.get("label_update"),
                })
                continue

            concept_id = _stable_candidate_id(task.job_id, candidate_key)
            entity = _build_entity(candidate, impression, concept_id)
            saved = upsert_entity(entity)
            result["facts"].append({
                "status": "created",
                "concept_id": saved.concept_id,
                "decision": decision,
            })
            if decision == "related" and target_id:
                # judge 目标通常刚被 resolver 读取过；提交前仍再做一次存在性
                # 检查，防止外部删除造成悬空图边。
                if read_entity(target_id) is not None:
                    related_to_create.append((
                        saved.concept_id,
                        target_id,
                        str(identity.get("relation_type") or "related_to"),
                        str(identity.get("relation_direction") or "symmetric"),
                    ))

        # 3. 已有认知情绪只能来自 affected_memories，以及 same 候选转换出的
        # 情绪信号；每次都按提交时数据库当前分数重算。
        for update in [
            *(effects.get("existing_memory_updates") or []),
            *same_emotion_updates,
        ]:
            result["emotion_updates"].append(
                _apply_existing_emotion_update(update)
            )

        # 4. 事实实体已经存在后，才建立 judge 确认的一跳关系。
        for source_id, target_id, relation_type, direction in related_to_create:
            relation = build_relation_for_candidate(
                source_id,
                target_id,
                relation_type,
                direction,
            )
            if relation is None:
                continue
            saved_relation = upsert_relation(relation)
            result["relations"].append({
                "relation_id": saved_relation.relation_id,
                "source_concept_id": saved_relation.source_concept_id,
                "relation_type": saved_relation.relation_type,
                "target_concept_id": saved_relation.target_concept_id,
            })

        return result
