import hashlib
from typing import Any

from langchain_core.documents import Document

from memory.schema import MemoryEntity
from memory.sql_store import (
    create_tables,
    delete_entity as sql_delete_entity,
    get_entity,
    search_by_name_or_alias,
    upsert_entity as sql_upsert_entity,
    update_entity_content as sql_update_entity_content,
    reinforce_entity as sql_reinforce_entity,
)
from memory.vector_store import vector_db
from memory.vector_repair_store import (
    enqueue as enqueue_vector_repair,
    list_pending as list_pending_vector_repairs,
    mark_attempt as mark_vector_repair_attempt,
    mark_failed as mark_vector_repair_failed,
    mark_repaired as mark_vector_repair_repaired,
)


def ensure_memory_store() -> None:
    """确保 SQLite 记忆表已创建。"""
    create_tables()


def _entity_snapshot(entity: MemoryEntity) -> dict[str, Any]:
    """把当前 SQLite 实体快照转成修复队列可保存的普通字典。"""
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
        "revision": entity.revision,
        "created_at": entity.created_at,
        "last_accessed_at": entity.last_accessed_at,
        "last_modified_at": entity.last_modified_at,
        "source": entity.source,
        "confidence": entity.confidence,
    }


def _sync_vector_index(entity: MemoryEntity) -> None:
    """把一个 SQLite 实体同步到 Chroma；失败由调用方记录修复任务。"""
    doc = _vector_document(entity)
    existing = vector_db.get(ids=[entity.concept_id])
    if existing and existing.get("ids"):
        vector_db.update_document(entity.concept_id, doc)
    else:
        vector_db.add_documents([doc], ids=[entity.concept_id])


def _sync_or_enqueue(entity: MemoryEntity, operation: str) -> None:
    """同步失败时记录待修复任务，然后继续抛出原始异常。"""
    try:
        _sync_vector_index(entity)
    except Exception as exc:
        try:
            enqueue_vector_repair(
                entity.concept_id,
                operation,
                entity_snapshot=_entity_snapshot(entity),
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            # 队列自身如果也暂时不可用，仍不能把真正的索引错误隐藏掉。
            pass
        raise exc


def build_genesis_entity(persona: dict[str, Any]) -> MemoryEntity | None:
    """把角色卡声明的初始关系记忆转换成结构化实体。

    ``knowledge_boundary`` 是参数知识权限，不属于这条记忆。这里只处理
    persona 明确提供的 ``genesis_memory``，避免把角色卡的领域白名单伪装成
    Agent 已经经历过的个人认知。
    """
    if not isinstance(persona, dict):
        return None

    genesis = persona.get("genesis_memory")
    if not isinstance(genesis, dict):
        return None

    user_role = str(persona.get("user_role") or "用户").strip()
    agent_name = str(persona.get("agent_name") or "Agent").strip()
    summary = str(genesis.get("summary") or "").strip()
    if not user_role or not summary:
        return None

    stable_key = f"{agent_name}|{user_role}|genesis".encode(
        "utf-8"
    )
    concept_id = "genesis_" + hashlib.sha256(stable_key).hexdigest()[:24]

    raw_tags = genesis.get("tags", [])
    if isinstance(raw_tags, str):
        tags = [item.strip() for item in raw_tags.replace("，", ",").split(",")]
    elif isinstance(raw_tags, list):
        tags = [str(item).strip() for item in raw_tags]
    else:
        tags = []

    return MemoryEntity(
        concept_id=concept_id,
        canonical_name=user_role,
        aliases=[user_role],
        memory_type="relationship",
        identity_signature={
            "subject": agent_name,
            "relation": "bound_to_user",
            "object": user_role,
            "qualifier": "genesis",
        },
        summary=summary,
        tags=[item for item in tags if item],
        emotion_score=float(genesis.get("emotion_score", 50.0)),
        emotion_label=str(genesis.get("emotion_label") or "中性").strip(),
        mention_count=1,
        source="genesis",
        confidence=1.0,
    )


def initialize_genesis_memory(persona: dict[str, Any]) -> MemoryEntity | None:
    """首次启动时写入初始认知；已有同一稳定 ID 时保持不变。"""
    entity = build_genesis_entity(persona)
    if entity is None:
        return None

    ensure_memory_store()
    existing = get_entity(entity.concept_id)
    if existing is not None:
        return existing

    return upsert_entity(entity)


def _vector_document(entity: MemoryEntity) -> Document:
    """把完整认知实体转换成 Chroma 只需要的向量文档。"""
    return Document(
        page_content=f"{entity.canonical_name}：{entity.summary}",
        metadata={
            "concept_id": entity.concept_id,
            "canonical_name": entity.canonical_name,
            "memory_type": entity.memory_type,
            "emotion_score": entity.emotion_score,
            "emotion_label": entity.emotion_label,
        },
    )


def upsert_entity(entity: MemoryEntity) -> MemoryEntity:
    """同时写入 SQLite 主库和 Chroma 向量索引。"""
    ensure_memory_store()

    # 先写 SQLite，保证结构化主库是权威来源。
    saved = sql_upsert_entity(entity)

    # Chroma 的 id 直接使用 concept_id，这样不会再依赖 concept_name。
    _sync_or_enqueue(saved, "upsert")

    return saved


def read_entity(concept_id: str) -> MemoryEntity | None:
    """从 SQLite 主库读取完整认知实体。"""
    ensure_memory_store()
    return get_entity(concept_id)


def reinforce_entity(concept_id: str) -> MemoryEntity | None:
    """原子增加引用次数，并同步更新 Chroma 文档。"""
    ensure_memory_store()
    saved = sql_reinforce_entity(concept_id)
    if saved is None:
        return None

    _sync_or_enqueue(saved, "reinforce")
    return saved


def update_entity_content(
    entity: MemoryEntity,
    *,
    expected_revision: int,
) -> MemoryEntity | None:
    """以乐观并发方式更新事实内容，并在成功后同步 Chroma。"""
    ensure_memory_store()
    saved = sql_update_entity_content(
        entity,
        expected_revision=expected_revision,
    )
    if saved is None:
        return None

    _sync_or_enqueue(saved, "update")
    return saved


def find_entities_by_name_or_alias(name: str) -> list[MemoryEntity]:
    """通过显示名或别名召回候选认知，可能返回多个。"""
    ensure_memory_store()
    return search_by_name_or_alias(name)


def delete_entity(concept_id: str) -> bool:
    """开发期删除实体，并尝试同步删除 Chroma 索引。"""
    ensure_memory_store()

    deleted = sql_delete_entity(concept_id)

    try:
        vector_db.delete(ids=[concept_id])
    except Exception as exc:
        try:
            enqueue_vector_repair(
                concept_id,
                "delete",
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            # 与实体同步相同：队列失败不能遮掉 Chroma 的原始错误。
            pass
        # SQLite 删除已经成功；仍把 Chroma 原始错误交给上层，避免伪装成
        # “索引也已删除”。
        raise exc

    return deleted


def repair_pending_vectors(limit: int | None = None) -> list[dict[str, Any]]:
    """按 SQLite 当前状态重建待修复的 Chroma 索引。

    修复时重新读取 SQLite，而不是信任失败瞬间保存的旧快照；这样后续
    新提交不会被一次迟到的修复覆盖。删除操作则以 SQLite 中实体不存在
    为准删除 Chroma 文档。
    """
    ensure_memory_store()
    results = []
    for task in list_pending_vector_repairs(limit):
        repair_id = task["repair_id"]
        mark_vector_repair_attempt(repair_id)
        try:
            current = get_entity(task["concept_id"])
            if current is None:
                vector_db.delete(ids=[task["concept_id"]])
            else:
                _sync_vector_index(current)
            mark_vector_repair_repaired(repair_id)
            results.append({"repair_id": repair_id, "status": "repaired"})
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            mark_vector_repair_failed(repair_id, error)
            results.append(
                {"repair_id": repair_id, "status": "failed", "error": error}
            )
    return results
