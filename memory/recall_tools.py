"""主 Agent 使用的只读回忆工具。

工具层不负责写入认知，也不负责重新判断身份。它只是把现有的
MemoryEntity 检索和 ExperienceSlice 时间查询包装成模型可调用的接口。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from langchain_core.tools import tool

from memory.experience_store import (
    get_experience_time_coverage,
    search_experience_slices,
)
from memory.retrieval_engine import retrieve_memories
from memory.sql_store import get_memory_time_coverage
from memory.store_manager import read_entity
from memory.experience_summary_store import list_summaries
from config import RECALL_MAX_CALLS_PER_TURN


_recall_budget: ContextVar[dict[str, int] | None] = ContextVar(
    "recall_memory_budget",
    default=None,
)


@contextmanager
def recall_call_budget(max_calls: int = RECALL_MAX_CALLS_PER_TURN):
    """为一次 Agent 回合建立独立的 recall_memory 调用计数。

    ContextVar 只在当前调用链生效，因此不同事件、不同线程不会互相消耗
    次数。工具被单独测试或由其他代码直接调用时没有预算上下文，仍保持
    原来的只读查询行为；主循环会在 agent.invoke 外层建立预算。
    """
    if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
        raise ValueError("max_calls 必须是大于等于 1 的整数")

    token = _recall_budget.set({"limit": max_calls, "count": 0})
    try:
        yield
    finally:
        _recall_budget.reset(token)


def _consume_recall_call() -> tuple[bool, int, int | None]:
    """消耗一次本轮配额，返回 (是否允许, 当前次数, 上限)。"""
    budget = _recall_budget.get()
    if budget is None:
        return True, 0, None

    if budget["count"] >= budget["limit"]:
        return False, budget["count"], budget["limit"]

    budget["count"] += 1
    return True, budget["count"], budget["limit"]


def _coverage(thread_id: str | None = None) -> dict[str, dict[str, Any]]:
    """读取两个权威数据表的实际时间覆盖范围。"""
    return {
        "memory_entities": get_memory_time_coverage(),
        "experience_slices": get_experience_time_coverage(thread_id=thread_id),
    }


def _entity_to_dict(entity: Any) -> dict[str, Any]:
    return {
        "concept_id": entity.concept_id,
        "canonical_name": entity.canonical_name,
        "aliases": list(entity.aliases),
        "memory_type": entity.memory_type,
        "identity_signature": dict(entity.identity_signature),
        "summary": entity.summary,
        "tags": list(entity.tags),
        "emotion_score": entity.emotion_score,
        "emotion_label": entity.emotion_label,
        "mention_count": entity.mention_count,
        "created_at": entity.created_at,
        "last_accessed_at": entity.last_accessed_at,
        "last_modified_at": entity.last_modified_at,
        "source": entity.source,
        "confidence": entity.confidence,
    }


def _slice_to_dict(stored: Any) -> dict[str, Any]:
    experience = stored.experience
    event = experience.perception_event
    return {
        "slice_id": experience.slice_id,
        "event_id": event.event_id,
        "thread_id": stored.thread_id,
        "event_sequence": stored.event_sequence,
        "occurred_at": event.occurred_at,
        "source": event.source,
        "modality": event.modality,
        "content": event.content,
        "response_or_actions": [
            action.to_dict() for action in experience.response_or_actions
        ],
        "observations": [
            observation.to_dict() for observation in experience.observations
        ],
        "perception_understanding": dict(experience.perception_understanding),
        "activated_memory_refs": [
            dict(ref) for ref in experience.activated_memory_refs
        ],
        "completed_at": experience.completed_at,
    }


def _summary_to_dict(summary: Any) -> dict[str, Any]:
    return {
        "summary_id": summary.summary_id,
        "summary_type": summary.summary_type,
        "start_at": summary.start_at,
        "end_at": summary.end_at,
        "source_slice_ids": list(summary.source_slice_ids),
        "summary_text": summary.summary_text,
        "uncertainties": list(summary.uncertainties),
        "generated_at": summary.generated_at,
        "algorithm_version": summary.algorithm_version,
        "prompt_version": summary.prompt_version,
    }


@tool
def recall_memory(
    query: str = "",
    concept_id: str = "",
    slice_id: str = "",
    start_at: str = "",
    end_at: str = "",
    thread_id: str = "",
    memory_type: str = "",
    subject: str = "",
    object: str = "",
    limit: int = 10,
    include_related: bool = True,
) -> str:
    """只读查询长期认知、情景摘要和具体经历。

    需要回忆过去时使用本工具。query 可写认知名称、事件中的具体内容，
    start_at/end_at 可限定时间，concept_id 可精确读取一个认知，
    thread_id 可限定对话线程。返回的 summaries 只是定位信息；需要还原具体过程时，
    应根据摘要的时间范围或 source_slice_ids 再查询 experiences。
    返回的 data_coverage 是当前数据库实际记录的时间范围统计，不代表范围之外没有历史。
    普通当前事件不需要调用本工具。工具不会创建、修改或删除任何记忆。
    """
    allowed, call_count, call_limit = _consume_recall_call()
    if not allowed:
        return json.dumps(
            {
                "query": str(query or "").strip(),
                "source_boundary": "只读；本轮主动回忆已达到代码设置的调用上限",
                "memories": [],
                "summaries": [],
                "experiences": [],
                "data_coverage": _coverage(),
                "recall_call": {
                    "status": "limit_reached",
                    "count": call_count,
                    "limit": call_limit,
                },
                "result_status": "call_limit_reached",
            },
            ensure_ascii=False,
        )

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        return json.dumps({"error": "limit 必须是 1 到 50 的整数"}, ensure_ascii=False)

    query = str(query or "").strip()
    concept_id = str(concept_id or "").strip()
    slice_id = str(slice_id or "").strip() or None
    start_at = str(start_at or "").strip() or None
    end_at = str(end_at or "").strip() or None
    thread_id = str(thread_id or "").strip() or None
    memory_type = str(memory_type or "").strip()
    subject = str(subject or "").strip()
    object_value = str(object or "").strip()

    memories = []
    if concept_id:
        entity = read_entity(concept_id)
        if entity is not None:
            memories.append(_entity_to_dict(entity))
    elif query or memory_type or subject or object_value:
        filters = {
            key: value
            for key, value in {
                "memory_type": memory_type,
                "subject": subject,
                "object": object_value,
            }.items()
            if value
        }
        result = retrieve_memories(
            query=query,
            filters=filters,
            top_k=limit,
            include_related=include_related,
        )
        memories = [
            {
                **_entity_to_dict(item.entity),
                "retrieval_sources": list(item.retrieval_sources),
                "vector_distance": item.vector_distance,
            }
            for item in result.accepted[:limit]
        ]

    experiences = []
    experience_selector_present = any(
        value is not None and value != ""
        for value in (query, slice_id, start_at, end_at, thread_id, concept_id)
    )
    if experience_selector_present:
        rows = search_experience_slices(
            # 只有 concept_id 时也要给 SQLite 一个选择条件；切片中的
            # activated_memory_refs 会保存这个 id，随后再做精确过滤。
            keyword=query or (concept_id if concept_id else None),
            slice_id=slice_id,
            start_at=start_at,
            end_at=end_at,
            thread_id=thread_id,
            limit=limit,
        )
        if concept_id:
            rows = [
                row for row in rows
                if any(
                    ref.get("concept_id") == concept_id
                    for ref in row.experience.activated_memory_refs
                )
            ]
        experiences = [_slice_to_dict(row) for row in rows]

    summaries = []
    if start_at or end_at or query:
        summary_rows = list_summaries(
            start_at=start_at,
            end_at=end_at,
            limit=min(100, limit * 5),
        )
        if query:
            summary_rows = [
                row for row in summary_rows
                if query in row.summary_text
                or any(query in item for item in row.uncertainties)
            ]
        summaries = [_summary_to_dict(row) for row in summary_rows[:limit]]

    return json.dumps(
        {
            "query": query,
            "source_boundary": "只读；认知来自 SQLite，经历来自不可变 ExperienceSlice，摘要只是导航索引",
            "memories": memories,
            "summaries": summaries,
            "experiences": experiences,
            "data_coverage": _coverage(thread_id),
            "recall_call": {
                "status": "allowed",
                "count": call_count,
                "limit": call_limit,
            },
            "result_status": "found" if memories or summaries or experiences else "no_match",
        },
        ensure_ascii=False,
    )
