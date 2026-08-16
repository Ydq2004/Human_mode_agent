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


def ensure_memory_store() -> None:
    """确保 SQLite 记忆表已创建。"""
    create_tables()


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

    doc = _vector_document(saved)

    # Chroma 的 id 直接使用 concept_id，这样不会再依赖 concept_name。
    existing = vector_db.get(ids=[saved.concept_id])
    if existing and existing.get("ids"):
        vector_db.update_document(saved.concept_id, doc)
    else:
        vector_db.add_documents([doc], ids=[saved.concept_id])

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

    doc = _vector_document(saved)
    existing = vector_db.get(ids=[saved.concept_id])
    if existing and existing.get("ids"):
        vector_db.update_document(saved.concept_id, doc)
    else:
        vector_db.add_documents([doc], ids=[saved.concept_id])
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

    doc = _vector_document(saved)
    existing = vector_db.get(ids=[saved.concept_id])
    if existing and existing.get("ids"):
        vector_db.update_document(saved.concept_id, doc)
    else:
        vector_db.add_documents([doc], ids=[saved.concept_id])
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
    except Exception:
        # Chroma 中不存在该 id 时，不影响 SQLite 删除结果。
        pass

    return deleted
