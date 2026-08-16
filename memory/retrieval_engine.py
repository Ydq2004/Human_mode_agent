from dataclasses import dataclass, field

from memory.schema import MemoryEntity
from memory.sql_store import list_entities, list_relations_for_entity, search_by_name_or_alias

from memory.store_manager import read_entity
from memory.vector_store import vector_db

VECTOR_ACCEPT_DISTANCE = 0.85
VECTOR_WEAK_DISTANCE = 1.10

@dataclass
class RetrievedMemory:
    """
    一条本轮检索结果。

    entity:
        SQLite 主库中的完整认知实体。
        这是最终可信的记忆内容。

    retrieval_sources:
        这条记忆通过哪些已接受的路径被召回。
        例如：
        ["alias"]
        ["filter_object"]
        ["vector"]
        ["alias", "filter_subject"]
        ["alias", "vector"]

    vector_distance:
        如果这条 accepted 结果由向量路径命中过，
        这里记录 Chroma 返回的最小向量距离。
        距离越小，通常表示 query 和向量文档越接近。

        注意：
        这个值只是召回证据，不是“同一认知”或“可写入关系”的判断结果。
        不能因为距离小，就直接合并记忆或写入关系。
    """
    entity: MemoryEntity
    retrieval_sources: list[str] = field(default_factory=list)
    vector_distance: float | None = None

@dataclass
class VectorCandidateDebug:
    """
    被向量召回但未进入主结果的候选。

    这个结构只用于调试和阈值校准。
    它不是长期记忆，不写入数据库，也不进入主 Agent 上下文。

    quality:
        当前只有 weak / rejected。
        weak 表示向量距离接近但未达到 accepted 阈值。
        rejected 表示距离更远，当前阶段直接过滤。
    """
    entity: MemoryEntity
    vector_distance: float
    quality: str  # weak / rejected

@dataclass
class RetrievalResult:
    """
    一次检索调用的完整结果。

    accepted:
        已通过当前检索接受层的结果。
        这些可以交给 context_builder 或 appraisal 继续使用。

    debug_vector_candidates:
        没有进入 accepted 的向量调试候选。
        里面可能包含 quality=weak 和 quality=rejected。

        它们用于调试、阈值校准、观察 query 是否需要 router 改写。

    注意：
    weak / rejected 候选不是长期记忆，也不是主 Agent 上下文。
    它们只是当前阶段用于观察向量召回质量的运行时信息。
    """
    accepted: list[RetrievedMemory] = field(default_factory=list)
    debug_vector_candidates: list[VectorCandidateDebug] = field(default_factory=list)

def _norm(value) -> str:
    """把 None、空值、数字等统一转成干净字符串，避免比较时出错。"""
    return str(value or "").strip()


def _signature_value(signature: dict, key: str) -> str:
    """
    从 identity_signature 里安全取值。

    这里不做语义理解，只做结构读取。
    如果 signature 不是 dict，说明上游数据不干净，返回空字符串。
    """
    if not isinstance(signature, dict):
        return ""
    return _norm(signature.get(key))

def _normalize_queries(query: str | list[str]) -> list[str]:
    """
    把单个 query 或多个 query 统一成列表。

    这里保留多个 query 的边界，
    不把多个语义主题重新拼成一段长文本。
    """
    if isinstance(query, str):
        queries = [query]
    elif isinstance(query, list):
        queries = query
    else:
        queries = []

    normalized = []

    for item in queries:
        item = _norm(item)
        if item and item not in normalized:
            normalized.append(item)

    return normalized

def _query_tokens(query: str) -> list[str]:
    """
    把 query 拆成可用于“名称/别名精确匹配”的 token。

    重要边界：
    - 这里不是分词器。
    - 这里不做语义召回。
    - 这里不把“主人”自动理解成 subject=主人。
    - 自然语言语义召回应由后续 vector 检索负责。
    """
    query = _norm(query)
    if not query:
        return []

    tokens = []
    for raw in query.replace("\n", " ").replace("，", " ").replace(",", " ").split():
        token = raw.strip()
        if token and token not in tokens:
            tokens.append(token)

    # 保留完整 query，让“张开双臂”这类完整 alias 有机会精确命中。
    if query not in tokens:
        tokens.insert(0, query)

    return tokens


def _add_candidate(
    candidates: dict[str, RetrievedMemory],
    entity: MemoryEntity,
    source: str,
) -> None:
    """
    把一个实体加入候选集合，并记录它是通过什么路径命中的。

    为什么要按 concept_id 合并：
    同一条认知可能同时通过 alias、filter_object、未来的 vector 命中。
    但主 Agent 不应该看到重复记忆。
    所以这里以 concept_id 作为唯一 key，把多路来源合并到 retrieval_sources。
    """
    existing = candidates.get(entity.concept_id)

    if existing is None:
        candidates[entity.concept_id] = RetrievedMemory(
            entity=entity,
            retrieval_sources=[source],
        )
        return

    if source not in existing.retrieval_sources:
        existing.retrieval_sources.append(source)


def _matches_filter_subject(entity: MemoryEntity, subject: str) -> bool:
    """结构化 subject 精确匹配。只有 filters 明确传入 subject 时才用。"""
    if not subject:
        return False
    signature = entity.identity_signature or {}
    return _signature_value(signature, "subject") == subject


def _matches_filter_object(entity: MemoryEntity, obj: str) -> bool:
    """结构化 object 精确匹配。只有 filters 明确传入 object 时才用。"""
    if not obj:
        return False
    signature = entity.identity_signature or {}
    return _signature_value(signature, "object") == obj

def _matches_memory_type_filter(
    entity: MemoryEntity,
    filters: dict | None,
) -> bool:
    """
    检查名称和向量路径当前支持的硬过滤条件。

    subject/object 由结构化检索路径单独用于直接召回；不能把 LLM 提供的
    自由文本值当成身份裁决，强行排除名称或向量已经命中的候选。
    """
    filters = filters or {}

    memory_type = _norm(filters.get("memory_type"))

    if memory_type and entity.memory_type != memory_type:
        return False

    return True


def _matches_structured_filters(
    entity: MemoryEntity,
    filters: dict | None,
) -> bool:
    """
    检查路径 2 的全部结构化条件。

    同一条 activation cue 中的过滤字段描述的是同一个目标，必须使用 AND。
    例如 subject=主人 且 object=月白稿时，不能因为只匹配 subject 就把所有
    关于主人的认知加入结果。只有一个字段时，则只检查该字段。
    """
    filters = filters or {}
    if not _matches_memory_type_filter(entity, filters):
        return False

    subject = _norm(filters.get("subject"))
    if subject and not _matches_filter_subject(entity, subject):
        return False

    obj = _norm(filters.get("object"))
    if obj and not _matches_filter_object(entity, obj):
        return False

    return True

def _candidate_pool(memory_type: str) -> list[MemoryEntity]:
    """
    生成结构化检索的候选池。

    memory_type 是过滤条件，不是身份判断。
    - 如果传入 memory_type，只扫描该类型，避免无意义扩大范围。
    - 如果没有传入，就扫描全部实体。
    """
    return list_entities(memory_type or None)

def _retrieve_vector_candidates(
    query: str,
    top_k: int,
    filters: dict|None=None,
) -> list[tuple[MemoryEntity, float]]:
    """
    使用 Chroma 做语义候选召回。

    这个函数只负责“找可能相关的 concept_id”。

    它不负责：
    - 判断是不是同一认知
    - 判断是不是 related
    - 修改数据库
    - 直接把 Chroma 文档当成最终记忆

    返回：
        [(MemoryEntity, vector_distance), ...]

    为什么返回 MemoryEntity 而不是 Document：
        Chroma 只保存向量索引和少量 metadata。
        SQLite 才保存完整的 aliases、identity_signature、summary、
        mention_count 和时间字段。
    """
    query = _norm(query)
    if not query or top_k <= 0:
        return []

    # 先召回候选。
    # 这里不使用旧流程的 MATCH_THRESHOLD/EDGE_THRESHOLD，
    # 因为不同 embedding 模型的距离分布不能直接照搬。
    vector_results = vector_db.similarity_search_with_score(
        query,
        k=top_k,
    )

    candidates = []
    seen_ids = set()

    for document, distance in vector_results:
        concept_id = _norm(document.metadata.get("concept_id"))

        # 没有 concept_id 的向量文档不具备新系统所需的稳定身份，
        # 不能直接进入新记忆检索结果。
        if not concept_id or concept_id in seen_ids:
            continue

        # SQLite 是权威来源。
        # 如果 Chroma 中有旧文档，但 SQLite 已不存在对应实体，
        # 说明索引过期，直接跳过。
        entity = read_entity(concept_id)
        if entity is None:
            continue

        # memory_type 过滤在回表后再确认。
        # 这样即使 Chroma metadata 过期，也不会把错误类型交给上层。
        if not _matches_memory_type_filter(entity, filters):
            continue

        seen_ids.add(concept_id)
        candidates.append((entity, float(distance)))

    return candidates

def _classify_vector_distance(distance: float) -> str:
    """
    对向量候选做阶段性质量分层。

    重要边界：
    - 这是检索候选过滤，不是身份判断。
    - accepted 不等于 same。
    - weak 不等于无关，只是暂时不进入主检索结果。
    - 阈值来自 Phase 0 当前测试集，后续必须继续校准。

    当前阈值只服务“是否进入检索结果”。
    它不负责判断 same / related / new。
    """
    if distance <= VECTOR_ACCEPT_DISTANCE:
        return "accepted"

    if distance <= VECTOR_WEAK_DISTANCE:
        return "weak"

    return "rejected"

def _expand_related_candidates(
    candidates: dict[str, RetrievedMemory],
) -> None:
    """
    基于已接受结果做一跳关系扩展。

    重要边界：
    - 只从 accepted candidates 出发。
    - 不从 debug_vector_candidates 出发。
    - 不做多跳图遍历。
    - 不判断关系是否语义正确。
    - 不写库。

    为什么只从 accepted 出发：
    weak/rejected 向量候选本身还不适合进入主结果，
    如果从它们继续扩展关系，会把噪声放大。
    """
    seed_ids = list(candidates.keys())

    for seed_id in seed_ids:
        for relation in list_relations_for_entity(seed_id):
            if relation.source_concept_id == seed_id:
                related_id = relation.target_concept_id
                source = f"related_out:{relation.relation_type}"
            else:
                related_id = relation.source_concept_id
                source = f"related_in:{relation.relation_type}"

            entity = read_entity(related_id)
            if entity is None:
                continue

            _add_candidate(candidates, entity, source)


def _direct_sources(item: RetrievedMemory) -> list[str]:
    """
    返回直接召回来源。

    related_out / related_in 只是关系扩展证据，
    优先级不能高于 alias、filter、vector 等直接召回。
    """
    return [
        source
        for source in item.retrieval_sources
        if not source.startswith("related_")
    ]


def retrieve_memories(
    query: str | list[str],
    filters: dict | None = None,
    top_k: int = 10,
    include_related: bool = True,
) ->  RetrievalResult:
    """
    Phase 0 混合检索引擎：结构化 + 向量召回版。

    当前版本做：
    1. query 或 query list -> canonical_name / aliases 精确匹配。
    2. query 或 query list -> Chroma 向量召回。
    3. 向量召回只取 concept_id，再回 SQLite 读取完整 MemoryEntity。
    4. filters.subject -> identity_signature.subject 精确匹配。
    5. filters.object -> identity_signature.object 精确匹配。
    6. filters.memory_type -> 限制结构化候选池和向量回表结果。
    7. 按 concept_id 合并去重。
    8. 对已接受的直接结果做一跳关系扩展。
    9. 返回 accepted 结果和未采用的向量调试候选。

    当前版本不做：
    - 身份裁决。
    - 数据库写入。
    - 主 Agent prompt 拼接。
    - 外部事实查询和个人记忆的来源适用性判断。

    include_related=False 可供调用方关闭一跳关系扩展。
    """
    filters = filters or {}
    queries = _normalize_queries(query)

    memory_type = _norm(filters.get("memory_type"))
    filter_subject = _norm(filters.get("subject"))
    filter_object = _norm(filters.get("object"))

    candidates: dict[str, RetrievedMemory] = {}
    debug_vector_candidates: list[VectorCandidateDebug] = []

   
    # 每个 query 单独跑 alias 和 vector 路径，避免多个语义主题互相稀释。
    for one_query in queries:
        # 路径 1：名称 / alias 精确匹配。
        for token in _query_tokens(one_query):
            for entity in search_by_name_or_alias(token):
                if not _matches_memory_type_filter(entity, filters):
                    continue
                _add_candidate(candidates, entity, "alias")

        # 路径 3：向量语义召回。
        # 向量结果先经过距离分层，只有 accepted 才进入主结果。
        # weak / rejected 只进入调试候选，帮助后续校准阈值和 router query。
        for entity, distance in _retrieve_vector_candidates(
            query=one_query,
            top_k=top_k,
           filters=filters,
        ):
            quality = _classify_vector_distance(distance)
            
            print(
                "vector_candidate",
                quality,
                entity.concept_id,
                entity.canonical_name,
                distance,
            )
            if quality != "accepted":
                debug_vector_candidates.append(
                    VectorCandidateDebug(
                        entity=entity,
                        vector_distance=distance,
                        quality=quality,
                    )
                )
                continue

            _add_candidate(candidates, entity, "vector")

            # 记录 accepted 向量命中的最小距离。
            # _add_candidate 只负责按 concept_id 合并来源，
            # 所以距离需要在合并后的 RetrievedMemory 上单独维护。
            retrieved = candidates[entity.concept_id]

            if (
                retrieved.vector_distance is None
                or distance < retrieved.vector_distance
            ):
                retrieved.vector_distance = distance

    # 路径 2：结构化 subject/object 匹配。
    # 同一份 filters 中的字段必须全部满足；不同 cue 仍由 context_builder
    # 分别检索后合并。这样过滤字段越多，结果只会收窄，不会反向扩大。
    if filter_subject or filter_object:
        for entity in _candidate_pool(memory_type):
            if not _matches_structured_filters(entity, filters):
                continue

            if filter_subject:
                _add_candidate(candidates, entity, "filter_subject")
            if filter_object:
                _add_candidate(candidates, entity, "filter_object")

    # 路径 4：一跳关系扩展。
    # 只基于已经 accepted 的候选扩展，不从 weak/rejected 向量候选扩展。
    if include_related and candidates:
        _expand_related_candidates(candidates)
    

    results = list(candidates.values())

    # Phase 0 先用简单、可解释的阶段性排序。
    # 多路来源命中优先，其次看向量距离证据，再看提及次数和时间。
    results.sort(
        key=lambda item: (
            # 直接命中的认知必须优先于仅通过一跳关系带回的邻居。
            1 if _direct_sources(item) else 0,
            # 多条独立路径同时命中，优先级最高。
            len(item.retrieval_sources),

            # 只有向量路径命中时，距离越小越优先。
            # 没有 vector_distance 的结构化结果不应该因为 None 报错。
            (
                1.0 / (1.0 + item.vector_distance)
                if item.vector_distance is not None
                else 0.0
            ),

            # 被多次提及的认知通常更稳定。
            item.entity.mention_count,

            # 最后用最近修改时间做稳定排序。
            item.entity.last_modified_at
            or item.entity.last_accessed_at
            or item.entity.created_at,
        ),
        reverse=True,
    )

    return RetrievalResult(
        accepted=results[:top_k],
        debug_vector_candidates=debug_vector_candidates,
    )
