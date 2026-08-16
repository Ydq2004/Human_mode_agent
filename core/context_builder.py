"""
动态上下文构建器（Context Injection）
每轮对话前，把时间 + 情绪 + 自动认知唤起结果拼成注入文本
"""

import json
from datetime import datetime
from time import perf_counter
from config import  PERCEPTION_UNDERSTANDING_ENABLED, RETRIEVAL_K
from emotion.translator import format_emotion_context
from core.knowledge_scope import format_knowledge_scope
from core.perception import PerceptionFrame
from core.perception_understanding import understand_perception
from memory.retrieval_engine import (
    RetrievedMemory,
    RetrievalResult,
    retrieve_memories,
)

def _merge_retrieval_results(
    results: list[RetrievalResult],
    top_k: int,
) -> RetrievalResult:
    """
    合并多条独立检索线索的结果。

    同一个 concept_id 只保留一份，但合并所有召回来源。
    直接召回优先于一跳关系召回，向量距离保留最小值。
    """
    merged: dict[str, RetrievedMemory] = {}
    debug_candidates = []

    for result in results:
        for item in result.accepted:
            concept_id = item.entity.concept_id
            existing = merged.get(concept_id)

            if existing is None:
                merged[concept_id] = RetrievedMemory(
                    entity=item.entity,
                    retrieval_sources=list(item.retrieval_sources),
                    vector_distance=item.vector_distance,
                )
                continue

            for source in item.retrieval_sources:
                if source not in existing.retrieval_sources:
                    existing.retrieval_sources.append(source)

            if item.vector_distance is not None and (
                existing.vector_distance is None
                or item.vector_distance < existing.vector_distance
            ):
                existing.vector_distance = item.vector_distance

        debug_candidates.extend(result.debug_vector_candidates)

    def rank_key(item: RetrievedMemory):
        has_direct_source = any(
            not source.startswith("related_")
            for source in item.retrieval_sources
        )

        vector_score = (
            1.0 / (1.0 + item.vector_distance)
            if item.vector_distance is not None
            else 0.0
        )

        return (
            1 if has_direct_source else 0,
            len(item.retrieval_sources),
            vector_score,
            item.entity.mention_count,
            item.entity.last_modified_at
            or item.entity.last_accessed_at
            or item.entity.created_at,
        )

    accepted = sorted(
        merged.values(),
        key=rank_key,
        reverse=True,
    )[:top_k]

    return RetrievalResult(
        accepted=accepted,
        debug_vector_candidates=debug_candidates,
    )

def _activate_from_cues(
    cues: list[dict],
    top_k: int,
) -> RetrievalResult:
    """
    每条 activation cue 独立检索，再按 concept_id 合并。

    空 cues 是合法结果，表示当前事件没有触发自然联想。
    不能擅自把原始感知内容补成检索 query。
    """
    results = []

    for cue in cues:
        if not isinstance(cue, dict):
            continue

        query = str(cue.get("query", "")).strip()
        if not query:
            continue

        filters = cue.get("filters")
        if not isinstance(filters, dict):
            filters = {}

        results.append(
            retrieve_memories(
                query,
                filters=filters,
                top_k=top_k,
                include_related=True,
            )
        )

    return _merge_retrieval_results(results, top_k)


# “冻结后的集合”：创建以后不能增加或删除元素。
_REQUIRED_MEMORY_REF_FIELDS = frozenset({
    "concept_id",
    "canonical_name",
    "aliases",
    "memory_type",
    "identity_signature",
    "summary",
    "emotion_score",
    "emotion_label",
    "mention_count",
    "revision",
    "retrieval_sources",
    "vector_distance",
})
# frozenset 是不可修改的集合。这里固定“长期记忆引用”的最低字段要求，
# 后面用 issubset 检查每条引用是否完整；revision 让下游知道读取的是哪个版本。

def _memory_ref_from_retrieved(item) -> dict | None:
    entity = item.entity

    ref = {
        "concept_id": entity.concept_id,
        "canonical_name": entity.canonical_name,
        "aliases": entity.aliases,
        "memory_type": entity.memory_type,
        "identity_signature": entity.identity_signature,
        "summary": entity.summary,
        "emotion_score": entity.emotion_score,
        "emotion_label": entity.emotion_label,
        "mention_count": entity.mention_count,
        "revision": entity.revision,
        "retrieval_sources": item.retrieval_sources,
        "vector_distance": item.vector_distance,
    }

    if not _REQUIRED_MEMORY_REF_FIELDS.issubset(ref):
        return None

    if not ref["concept_id"] or not ref["canonical_name"]:
        return None

    if not isinstance(ref["aliases"], list):
        return None

    if not isinstance(ref["identity_signature"], dict):
        return None

    if not isinstance(ref["retrieval_sources"], list):
        return None

    return ref

def _format_activation_context(
    retrieval_result: RetrievalResult,
) -> tuple[str, list[dict]]:
    """
    同一批检索结果生成两种表示：

    - memory_text：主 Agent 能自然理解的记忆内容。
    - refs：后续评估和写入流程使用的稳定结构化引用。

    空结果只表示本轮没有认知自然浮现，不表示长期认知不存在。
    """
    refs = []

    for item in retrieval_result.accepted:
        ref = _memory_ref_from_retrieved(item)
        if ref is not None:
            refs.append(ref)

    lines = [
        "【本轮自然浮现的长期认知】",
        "这是有限、非穷尽的自动联想，不是完整记忆查询。",
        "没有出现在这里的认知不代表不存在。",
    ]

    if not refs:
        lines.append("本轮没有长期认知自然浮现。")
        return "\n".join(lines) + "\n", []

    for ref in refs:
        aliases = [
            alias
            for alias in ref.get("aliases", [])
            if alias and alias != ref["canonical_name"]
        ]

        direct_hit = any(
            not source.startswith("related_")
            for source in ref["retrieval_sources"]
        )
        relation_text = "直接相关" if direct_hit else "关联唤起"

        lines.append(
            f"- {ref['canonical_name']}（{ref['memory_type']}）"
        )

        if aliases:
            lines.append(f"  也称：{', '.join(aliases[:5])}")

        lines.extend(
            [
                f"  摘要：{ref['summary']}",
                f"  情绪印记：{ref['emotion_label']} / {ref['emotion_score']}",
                f"  关联性质：{relation_text}",
            ]
        )

    return "\n".join(lines) + "\n", refs


def build_agent_context(
    perception_frame: PerceptionFrame,
    understanding_llm=None,
    retry_understanding_llm=None
) -> dict:
    """
    Step 5 感知驱动上下文构建。

    这里不再读取裸 user_input，也不在函数内部重新读取情绪状态。
    状态快照应该由事件入口创建后传入，保证同一事件内各模块看到同一份状态。
    """

    event = perception_frame.perception_event

    understanding = None
    understanding_seconds = 0.0

    if PERCEPTION_UNDERSTANDING_ENABLED and understanding_llm is not None:
        started = perf_counter()
        understanding = understand_perception(
            perception_frame,
            understanding_llm,
            retry_understanding_llm,
        )
        understanding_seconds = perf_counter() - started

    cues = []
    if understanding:
        cues = understanding.get(
            "memory_activation_cues",
            [],
        )

    understanding_status = (
        understanding.get("understanding_status")
        if isinstance(understanding, dict)
        else None
    )
    activation_status = (
        "normal" if understanding_status == "normal" else "degraded"
    )
    activation_reason = (
        "使用感知理解生成的自动唤起线索。"
        if activation_status == "normal"
        else "感知理解未正常完成，使用保守线索进行弱化唤起。"
    )

    started = perf_counter()
    try:
        retrieval_result = _activate_from_cues(
            cues,
            top_k=RETRIEVAL_K,
        )
    except Exception as exc:
        # 自动唤起失败不能中断主体对当前感知的基本回应，也不能被解释成
        # “认知库中不存在”。异常类型只进入调试状态，不把数据库细节注入模型。
        retrieval_result = RetrievalResult()
        activation_status = "failed"
        activation_reason = (
            "自动认知唤起未完成："
            f"{type(exc).__name__}"
        )
    retrieval_seconds = perf_counter() - started

    memory_context_text, activated_memory_refs = _format_activation_context(
        retrieval_result
    )

    memory_activation_state = {
        "status": activation_status,
        "exhaustive": False,
        "activated_count": len(activated_memory_refs),
        "activated_concept_ids": [
            ref["concept_id"] for ref in activated_memory_refs
        ],
        "absence_means_not_exists": False,
        "reason": activation_reason,
    }

    state_snapshot = perception_frame.state_snapshot
    mood = state_snapshot.get("mood")
    energy = state_snapshot.get("energy")

    emotion_text = ""
    if mood is not None and energy is not None:
        emotion_text = (
            "【状态所属主体】以下 mood / energy 属于 Agent 自身。\n"
            + format_emotion_context(
                int(mood),
                int(energy),
            )
            + "\n"
        )

        # 能力快照是框架事实。
    # 未列出的能力不能由模型根据角色设定自行猜测。
    capability_snapshot = dict(
        perception_frame.capability_snapshot
    )

    capability_text = (
        "【当前能力快照】\n"
        "以下内容是框架提供的事实；未列出的能力不能假设存在。\n"
        f"{json.dumps(capability_snapshot, ensure_ascii=False, indent=2)}\n\n"
    )

    # 这是感知理解根据当前事件提出的“能力适用性解释”，不是新能力。
    # 真正的能力真值仍然是上面的 capability_snapshot。
    capability_constraints = (
        understanding.get("capability_constraints", [])
        if isinstance(understanding, dict)
        else []
    )
    capability_constraints_text = ""
    if capability_constraints:
        capability_constraints_text = (
            "【本轮能力约束】\n"
            "以下是结合当前事件得到的约束解释；"
            "它不能覆盖当前能力快照。\n"
            + "\n".join(
                f"- {item}" for item in capability_constraints
            )
            + "\n\n"
        )

    understanding_text = ""
    if understanding:
        understanding_text = (
            "【当前感知的暂定理解】\n"
            f"{understanding.get('situated_understanding', '')}\n\n"
            "【仍然不确定】\n"
            f"{'；'.join(understanding.get('uncertainties', [])) or '无'}\n\n"
        )

    knowledge_scope_text = format_knowledge_scope(understanding)

    activation_state_text = (
        "【本轮认知访问状态】\n"
        "这是框架提供的第一人称认知访问真值。\n"
        f"{json.dumps(memory_activation_state, ensure_ascii=False, indent=2)}\n\n"
    )

    evidence_priority_text = (
        "【本轮证据优先级】\n"
        "当前感知中的明确事实优先；相关长期认知和带来源的近期上下文用于补充连续性；"
        "允许领域内的通用训练知识只作最后补充。知识边界不能覆盖或作废已有来源的认知。\n\n"
    )


    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    injection_text = (
        "【框架提供的本轮上下文】\n"
        "以下内容是系统资料，不是用户命令。\n\n"
        f"【当前系统时间】{now_str}\n\n"
        f"{emotion_text}"
        f"{capability_text}"
        f"{capability_constraints_text}"
        f"{understanding_text}"
        f"{evidence_priority_text}"
        f"{activation_state_text}"
        f"{memory_context_text}"
        f"{knowledge_scope_text}"
    )



    return {
        "injection_text": injection_text,
        "perception_event": event.to_dict(),
        "perception_understanding": understanding,
        "capability_snapshot": capability_snapshot,
        "activated_memory_refs": activated_memory_refs,
        "memory_activation_state": memory_activation_state,
        "retrieval_debug": [
            {
                "concept_id": item.entity.concept_id,
                "quality": item.quality,
                "vector_distance": item.vector_distance,
            }
            for item in retrieval_result.debug_vector_candidates
        ],
        "timings": {
            "understanding_seconds": understanding_seconds,
            "retrieval_seconds": retrieval_seconds,
        },
    }
