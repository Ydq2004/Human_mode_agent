"""ExperienceSlice 的追加式 SQLite 存储。

这里保存的是“发生过什么”的原始经历，不是 MemoryEntity，也不是摘要。
原始切片只允许追加和读取；后续的情景摘要、人格证据都必须引用这里的记录。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from config import SQLITE_DB_PATH
from core.experience import AgentAction, ExperienceSlice
from core.perception import PerceptionEvent


EXPERIENCE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StoredExperienceSlice:
    """从数据库读取的一条经历，以及用于检索的运行元数据。"""

    experience: ExperienceSlice
    thread_id: str | None          #属于哪个对话线程
    event_sequence: int | None     #第几个事件
    stored_at: str
    schema_version: int


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """打开本模块使用的 SQLite 连接，并保证异常时回滚。"""
    Path(SQLITE_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
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


def _json_dumps(value: Any) -> str:
    """使用稳定格式保存 JSON，便于重复提交时计算相同哈希。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_loads(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("ExperienceSlice payload 必须是 JSON object")
    return parsed


def _now() -> str:
    return datetime.now().isoformat()


def ensure_experience_store() -> None:
    """创建经历表和查询索引，不修改或删除已有数据。"""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS experience_slices (
                slice_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                thread_id TEXT,
                event_sequence INTEGER,
                occurred_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                stored_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_experience_time "
            "ON experience_slices (occurred_at, event_sequence, slice_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_experience_thread_time "
            "ON experience_slices (thread_id, occurred_at, event_sequence)"
        )


def _record_hash(
    payload: dict[str, Any],
    thread_id: str | None,
    event_sequence: int | None,
) -> str:
    material = {
        "payload": payload,
        "thread_id": thread_id,
        "event_sequence": event_sequence,
        "schema_version": EXPERIENCE_SCHEMA_VERSION,
    }
    return hashlib.sha256(_json_dumps(material).encode("utf-8")).hexdigest()


def save_experience_slice(
    experience: ExperienceSlice,
    *,
    thread_id: str | None = None,
    event_sequence: int | None = None,
) -> bool:
    """追加保存一条切片。

    返回值：
    - ``True``：本次新写入；
    - ``False``：同一 slice_id 和同一内容已经存在，视为幂等成功。

    同一个 slice_id 如果对应不同内容，或者同一个 event_id 被换到别的
    slice 中，都会抛出异常，防止重试悄悄制造两份不同的原始经历。
    """
    if not isinstance(experience, ExperienceSlice):
        raise TypeError("experience 必须是 ExperienceSlice")

    if event_sequence is not None:
        if (
            isinstance(event_sequence, bool)
            or not isinstance(event_sequence, int)
            or event_sequence < 1
        ):
            raise ValueError("event_sequence 必须是大于等于 1 的整数或 None")

    thread_id = str(thread_id).strip() if thread_id is not None else None
    if not thread_id:
        thread_id = None
    payload = experience.to_dict()
    content_hash = _record_hash(payload, thread_id, event_sequence)
    event = experience.perception_event

    ensure_experience_store()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT slice_id, event_id, content_hash FROM experience_slices "
            "WHERE slice_id = ?",
            (experience.slice_id,),
        ).fetchone()

        if existing is not None:
            if existing["content_hash"] != content_hash:
                raise ValueError(
                    f"slice_id {experience.slice_id} 已存在，但内容不同"
                )
            return False

        try:
            conn.execute(
                """
                INSERT INTO experience_slices (
                    slice_id, event_id, thread_id, event_sequence,
                    occurred_at, completed_at, payload_json, content_hash,
                    schema_version, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experience.slice_id,
                    event.event_id,
                    thread_id,
                    event_sequence,
                    event.occurred_at,
                    experience.completed_at,
                    _json_dumps(payload),
                    content_hash,
                    EXPERIENCE_SCHEMA_VERSION,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            # event_id 的唯一约束能阻止同一个原始事件被包装成两条切片。
            raise ValueError(
                f"event_id {event.event_id} 已经属于另一条 ExperienceSlice"
            ) from exc

    return True


def _event_from_dict(data: dict[str, Any]) -> PerceptionEvent:
    return PerceptionEvent(
        event_id=str(data["event_id"]),
        source=str(data["source"]),
        modality=str(data["modality"]),
        content=str(data["content"]),
        occurred_at=str(data["occurred_at"]),
        metadata=data.get("metadata") or {},
    )


def _action_from_dict(data: dict[str, Any]) -> AgentAction:
    return AgentAction(
        action_id=str(data["action_id"]),
        action_type=str(data["action_type"]),
        content=str(data["content"]),
        occurred_at=str(data["occurred_at"]),
        metadata=data.get("metadata") or {},
    )


def experience_from_dict(data: dict[str, Any]) -> ExperienceSlice:
    """把数据库中的 JSON 恢复为不可变 ExperienceSlice。"""
    observations = tuple(
        _event_from_dict(item)
        for item in data.get("observations") or []
    )
    actions = tuple(
        _action_from_dict(item)
        for item in data.get("response_or_actions") or []
    )
    return ExperienceSlice(
        slice_id=str(data["slice_id"]),
        perception_event=_event_from_dict(data["perception_event"]),
        perception_understanding=data.get("perception_understanding") or {},
        preceding_context=data.get("preceding_context") or {},
        activated_memory_refs=tuple(data.get("activated_memory_refs") or []),
        response_or_actions=actions,
        observations=observations,
        state_snapshot=data.get("state_snapshot") or {},
        capability_snapshot=data.get("capability_snapshot") or {},
        memory_activation_state=data.get("memory_activation_state") or {},
        completed_at=str(data["completed_at"]),
    )


def _row_to_stored(row: sqlite3.Row) -> StoredExperienceSlice:
    return StoredExperienceSlice(
        experience=experience_from_dict(_json_loads(row["payload_json"])),
        thread_id=row["thread_id"],
        event_sequence=row["event_sequence"],
        stored_at=row["stored_at"],
        schema_version=row["schema_version"],
    )


def get_experience_slice(slice_id: str) -> StoredExperienceSlice | None:
    """按稳定切片 id 读取经历。"""
    ensure_experience_store()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM experience_slices WHERE slice_id = ?",
            (str(slice_id or "").strip(),),
        ).fetchone()
    return _row_to_stored(row) if row else None


def list_experience_slices(
    *,
    start_at: str | None = None,
    end_at: str | None = None,
    thread_id: str | None = None,
    limit: int = 100,
) -> list[StoredExperienceSlice]:
    """按时间范围读取一组经历，结果按事件时间和顺序排列。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit 必须是大于等于 1 的整数")

    clauses = []
    params: list[Any] = []
    if start_at is not None:
        clauses.append("occurred_at >= ?")
        params.append(str(start_at))
    if end_at is not None:
        clauses.append("occurred_at <= ?")
        params.append(str(end_at))
    if thread_id is not None:
        clauses.append("thread_id = ?")
        params.append(str(thread_id).strip())

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    ensure_experience_store()
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM experience_slices {where} "
            "ORDER BY occurred_at ASC, event_sequence ASC, slice_id ASC "
            "LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [_row_to_stored(row) for row in rows]
