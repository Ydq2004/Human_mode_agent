import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from config import SQLITE_DB_PATH
from memory.schema import MemoryEntity, MemoryRelation
from memory.types import ALLOWED_MEMORY_TYPES, ALLOWED_RELATION_TYPES, MemoryType


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """打开可按字段名读取、且退出时一定关闭的 SQLite 连接。"""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    """统一生成 ISO 时间字符串，方便之后排序和解析。"""
    return datetime.now().isoformat()


def _json_dumps(value) -> str:
    """把 list/dict 存成 JSON 字符串，保留中文。"""
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: str | None, default):
    """从 JSON 字符串恢复 list/dict；解析失败时返回默认值。"""
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def create_tables() -> None:
    """创建 Phase 0 记忆系统需要的结构化表。"""
    # Card_slot 可以整体替换；首次启动一个新实例时，memory_db 目录可能
    # 尚不存在。这里只补目录和表，不删除或重建任何已有实例数据。
    Path(SQLITE_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_entities (
                concept_id TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                aliases TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                identity_signature TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags TEXT NOT NULL,
                emotion_score REAL NOT NULL,
                emotion_label TEXT NOT NULL,
                mention_count INTEGER NOT NULL DEFAULT 1,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_accessed_at TEXT NOT NULL,
                last_modified_at TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL NOT NULL
            )
            """
        )

        # IF NOT EXISTS 不会给旧表补列，因此对已有实例做幂等迁移。
        entity_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(memory_entities)")
        }
        # 旧数据库可能没有新字段。ALTER TABLE 只补这一列，保留原有数据。
        if "revision" not in entity_columns:
            conn.execute(
                "ALTER TABLE memory_entities "
                "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_relations (
                relation_id TEXT PRIMARY KEY,
                source_concept_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                target_concept_id TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                last_reinforced_at TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                FOREIGN KEY (source_concept_id) REFERENCES memory_entities(concept_id),
                FOREIGN KEY (target_concept_id) REFERENCES memory_entities(concept_id)
            )
            """
        )

        # 常用查询索引：按名字、类型、关系端点查询。
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_entities_name ON memory_entities(canonical_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_entities_type ON memory_entities(memory_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_relations_source ON memory_relations(source_concept_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_relations_target ON memory_relations(target_concept_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_relations_type ON memory_relations(relation_type)")


def normalize_entity(entity: MemoryEntity) -> MemoryEntity:
    """写入前清洗实体，保证核心字段稳定、类型合法。"""
    now = _now()

    aliases = []
    for alias in entity.aliases:
        alias = str(alias).strip()
        if alias and alias not in aliases:
            aliases.append(alias)

    # 显示名必须能作为自己的别名被查到。
    if entity.canonical_name not in aliases:
        aliases.insert(0, entity.canonical_name)

    if entity.memory_type not in ALLOWED_MEMORY_TYPES:
        entity.memory_type = MemoryType.OTHER.value

    entity.aliases = aliases
    entity.tags = list(dict.fromkeys(str(x).strip() for x in entity.tags if str(x).strip()))
    entity.emotion_score = round(
        max(0.0, min(100.0, float(entity.emotion_score))),
        2,
    )
    entity.mention_count = max(1, int(entity.mention_count))
    entity.revision = max(1, int(entity.revision))
    entity.created_at = entity.created_at or now
    entity.last_accessed_at = entity.last_accessed_at or now
    entity.last_modified_at = entity.last_modified_at or now
    entity.confidence = max(0.0, min(1.0, float(entity.confidence)))

    return entity


def row_to_entity(row: sqlite3.Row) -> MemoryEntity:
    """把 SQLite 行数据恢复成 MemoryEntity。"""
    return MemoryEntity(
        concept_id=row["concept_id"],
        canonical_name=row["canonical_name"],
        aliases=_json_loads(row["aliases"], []),
        memory_type=row["memory_type"],
        identity_signature=_json_loads(row["identity_signature"], {}),
        summary=row["summary"],
        tags=_json_loads(row["tags"], []),
        emotion_score=row["emotion_score"],
        emotion_label=row["emotion_label"],
        mention_count=row["mention_count"],
        revision=row["revision"],
        created_at=row["created_at"],
        last_accessed_at=row["last_accessed_at"],
        last_modified_at=row["last_modified_at"],
        source=row["source"],
        confidence=row["confidence"],
    )


def upsert_entity(entity: MemoryEntity) -> MemoryEntity:
    """按 concept_id 新建或更新认知实体。"""
    entity = normalize_entity(entity)

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO memory_entities (
                concept_id, canonical_name, aliases, memory_type, identity_signature,
                summary, tags, emotion_score, emotion_label, mention_count,
                revision, created_at, last_accessed_at, last_modified_at,
                source, confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(concept_id) DO UPDATE SET
                canonical_name=excluded.canonical_name,
                aliases=excluded.aliases,
                memory_type=excluded.memory_type,
                identity_signature=excluded.identity_signature,
                summary=excluded.summary,
                tags=excluded.tags,
                emotion_score=excluded.emotion_score,
                emotion_label=excluded.emotion_label,
                mention_count=excluded.mention_count,
                revision=excluded.revision,
                last_accessed_at=excluded.last_accessed_at,
                last_modified_at=excluded.last_modified_at,
                source=excluded.source,
                confidence=excluded.confidence
            """,
            (
                entity.concept_id,
                entity.canonical_name,
                _json_dumps(entity.aliases),
                entity.memory_type,
                _json_dumps(entity.identity_signature),
                entity.summary,
                _json_dumps(entity.tags),
                entity.emotion_score,
                entity.emotion_label,
                entity.mention_count,
                entity.revision,
                entity.created_at,
                entity.last_accessed_at,
                entity.last_modified_at,
                entity.source,
                entity.confidence,
            ),
        )

    return entity


def update_entity_content(
    entity: MemoryEntity,
    *,
    expected_revision: int,
) -> MemoryEntity | None:
    """仅当版本仍匹配时，原子更新整合认知并令 revision 加一。"""
    # expected_revision 是上层读取这条认知时看到的版本。
    # WHERE 同时检查版本，避免用旧快照覆盖别人刚写入的新内容。
    entity = normalize_entity(entity)
    expected_revision = max(1, int(expected_revision))
    now = _now()

    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE memory_entities
            SET canonical_name = ?, aliases = ?, memory_type = ?,
                identity_signature = ?, summary = ?, tags = ?,
                mention_count = ?, revision = revision + 1,
                last_accessed_at = ?, last_modified_at = ?,
                source = ?, confidence = ?
            WHERE concept_id = ? AND revision = ?
            """,
            (
                entity.canonical_name,
                _json_dumps(entity.aliases),
                entity.memory_type,
                _json_dumps(entity.identity_signature),
                entity.summary,
                _json_dumps(entity.tags),
                entity.mention_count,
                now,
                now,
                entity.source,
                entity.confidence,
                entity.concept_id,
                expected_revision,
            ),
        )
        # rowcount 为 0 表示 concept_id 不存在，或版本已经变化。
        # 两种情况都不能假装更新成功；上层会重新读取后再裁决。
        if cursor.rowcount == 0:
            return None

    return get_entity(entity.concept_id)


def reinforce_entity(concept_id: str) -> MemoryEntity | None:
    """在 SQLite 事务内原子增加一次 mention_count。

    不能用“读取实体 -> Python 加一 -> 整体 upsert”模拟 reinforce，
    因为未来若有其他提交者会发生丢失更新。Step 6 目前只有一个提交线程，
    这里仍直接使用 SQL 的 ``mention_count = mention_count + 1``，把正确的
    存储语义固定下来。
    """
    # 让数据库自己执行“加一”，整个 UPDATE 是一次原子操作。
    # 这样不会出现“先读旧值、再在 Python 中加一、最后写回”造成的丢更新。
    concept_id = str(concept_id or "").strip()
    if not concept_id:
        return None

    now = _now()
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE memory_entities
            SET mention_count = mention_count + 1,
                last_accessed_at = ?,
                last_modified_at = ?
            WHERE concept_id = ?
            """,
            (now, now, concept_id),
        )
        if cursor.rowcount == 0:
            return None

    return get_entity(concept_id)


def get_entity(concept_id: str) -> MemoryEntity | None:
    """通过稳定 ID 读取认知实体。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM memory_entities WHERE concept_id = ?",
            (concept_id,),
        ).fetchone()

    return row_to_entity(row) if row else None


def search_by_name_or_alias(name: str) -> list[MemoryEntity]:
    """通过显示名或别名召回候选认知。名字/别名不保证唯一。"""
    target = name.strip()
    if not target:
        return []

    matches = []

    with _connect() as conn:
        rows = conn.execute("SELECT * FROM memory_entities").fetchall()

    for row in rows:
        entity = row_to_entity(row)
        if entity.canonical_name == target or target in entity.aliases:
            matches.append(entity)

    return matches

def list_entities(memory_type: str | None = None) -> list[MemoryEntity]:
    """列出认知实体。Phase 0 先用于 signature 候选召回。"""
    with _connect() as conn:
        if memory_type:
            rows = conn.execute(
                "SELECT * FROM memory_entities WHERE memory_type = ?",
                (memory_type,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM memory_entities").fetchall()

    return [row_to_entity(row) for row in rows]

def add_alias(concept_id: str, alias: str) -> MemoryEntity | None:
    """给已有认知增加一个别名。"""
    entity = get_entity(concept_id)
    if not entity:
        return None

    alias = alias.strip()
    if alias and alias not in entity.aliases:
        entity.aliases.append(alias)
        entity.last_modified_at = _now()
        return upsert_entity(entity)

    return entity


def delete_entity(concept_id: str) -> bool:
    """开发期删除认知实体，同时清理与它相关的一跳关系。"""
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM memory_entities WHERE concept_id = ?",
            (concept_id,),
        )
        conn.execute(
            "DELETE FROM memory_relations WHERE source_concept_id = ? OR target_concept_id = ?",
            (concept_id, concept_id),
        )

    return cursor.rowcount > 0


def normalize_relation(relation: MemoryRelation) -> MemoryRelation:
    """
    写入前清洗一跳关系。

    这里保护的是关系表的结构边界：
    - relation_id / source / target 必须存在。
    - relation_type 必须属于 Phase 0 允许集合。
    - weight / confidence 限制在可解释范围内。
    - 时间字段缺失时补当前时间。

    注意：
    这里不判断“这条关系语义是否正确”。
    关系语义应由 Step 6 写入前统一处理。
    """
    now = _now()

    relation.relation_id = str(relation.relation_id or "").strip()
    relation.source_concept_id = str(relation.source_concept_id or "").strip()
    relation.target_concept_id = str(relation.target_concept_id or "").strip()
    relation.relation_type = str(relation.relation_type or "").strip()

    if relation.relation_type not in ALLOWED_RELATION_TYPES:
        relation.relation_type = "related_to"

    relation.weight = max(0.0, min(1.0, float(relation.weight)))
    relation.confidence = max(0.0, min(1.0, float(relation.confidence)))
    relation.created_at = relation.created_at or now
    relation.last_reinforced_at = relation.last_reinforced_at or now

    return relation


def row_to_relation(row: sqlite3.Row) -> MemoryRelation:
    """把 SQLite 行数据恢复成 MemoryRelation。"""
    return MemoryRelation(
        relation_id=row["relation_id"],
        source_concept_id=row["source_concept_id"],
        relation_type=row["relation_type"],
        target_concept_id=row["target_concept_id"],
        weight=row["weight"],
        created_at=row["created_at"],
        last_reinforced_at=row["last_reinforced_at"],
        confidence=row["confidence"],
    )


def upsert_relation(relation: MemoryRelation) -> MemoryRelation:
    """
    新建或更新一跳关系。

    Phase 0 的关系是检索辅助结构。
    它不能替代 identity_signature，也不能替代 resolver/judge。
    """
    relation = normalize_relation(relation)

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO memory_relations (
                relation_id, source_concept_id, relation_type, target_concept_id,
                weight, created_at, last_reinforced_at, confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(relation_id) DO UPDATE SET
                source_concept_id=excluded.source_concept_id,
                relation_type=excluded.relation_type,
                target_concept_id=excluded.target_concept_id,
                weight=excluded.weight,
                last_reinforced_at=excluded.last_reinforced_at,
                confidence=excluded.confidence
            """,
            (
                relation.relation_id,
                relation.source_concept_id,
                relation.relation_type,
                relation.target_concept_id,
                relation.weight,
                relation.created_at,
                relation.last_reinforced_at,
                relation.confidence,
            ),
        )

    return relation


def list_relations_for_entity(concept_id: str) -> list[MemoryRelation]:
    """
    读取某个认知实体的一跳关系。

    这里同时读取：
    - source_concept_id = concept_id 的出边
    - target_concept_id = concept_id 的入边

    原因：
    Phase 0 的检索扩展只关心“一跳相关认知”，
    不在这里做复杂方向推理。
    方向语义仍然保存在 relation 的 source/target 字段里。
    """
    concept_id = str(concept_id or "").strip()
    if not concept_id:
        return []

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM memory_relations
            WHERE source_concept_id = ? OR target_concept_id = ?
            """,
            (concept_id, concept_id),
        ).fetchall()

    return [row_to_relation(row) for row in rows]
