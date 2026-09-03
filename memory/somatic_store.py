"""模拟身体状态的实例级 SQLite 存储。

身体状态是运行真值，不是角色卡文本。状态更新和事件日志在同一个事务中完成，
避免重复工具调用把同一个动作应用两次，也避免并发写入覆盖新状态。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from config import SQLITE_DB_PATH
from core.somatic_simulation import SomaticState


DEFAULT_SOMATIC_STATE = {"arousal": 0.5, "sensitivity": 0.5}


@dataclass(frozen=True)
class StoredSomaticState:
    persona_instance_id: str
    thread_id: str
    state: SomaticState
    revision: int
    last_operation_id: str | None
    updated_at: str


@dataclass(frozen=True)
class SomaticCommitResult:
    status: str
    record: StoredSomaticState
    operation: dict[str, Any]
    duplicate: bool = False


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
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


def _now() -> str:
    return datetime.now().isoformat()


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("身体状态存储内容必须是 JSON object")
    return parsed


def _identity(persona_instance_id: str, thread_id: str) -> tuple[str, str]:
    instance_id = str(persona_instance_id or "").strip()
    thread = str(thread_id or "").strip()
    if not instance_id or not thread:
        raise ValueError("persona_instance_id 和 thread_id 不能为空")
    return instance_id, thread


def ensure_somatic_store() -> None:
    """幂等创建当前状态表和追加式操作日志。"""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS somatic_states (
                persona_instance_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                state_json TEXT NOT NULL,
                revision INTEGER NOT NULL,
                last_operation_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (persona_instance_id, thread_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS somatic_operations (
                operation_id TEXT PRIMARY KEY,
                persona_instance_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                state_before_json TEXT NOT NULL,
                state_after_json TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                appraisal_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                resulting_revision INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_somatic_operations_instance_thread "
            "ON somatic_operations (persona_instance_id, thread_id, created_at)"
        )


def _state_from_row(row: sqlite3.Row) -> StoredSomaticState:
    return StoredSomaticState(
        persona_instance_id=row["persona_instance_id"],
        thread_id=row["thread_id"],
        state=SomaticState(_object(row["state_json"])),
        revision=int(row["revision"]),
        last_operation_id=row["last_operation_id"],
        updated_at=row["updated_at"],
    )


def get_somatic_state(
    persona_instance_id: str,
    thread_id: str,
    *,
    create: bool = True,
) -> StoredSomaticState:
    """读取当前身体状态；首次读取时保存一份明确的默认状态。"""
    instance_id, thread = _identity(persona_instance_id, thread_id)
    ensure_somatic_store()
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM somatic_states "
            "WHERE persona_instance_id = ? AND thread_id = ?",
            (instance_id, thread),
        ).fetchone()
        if row is None and create:
            conn.execute(
                """
                INSERT INTO somatic_states (
                    persona_instance_id, thread_id, state_json, revision,
                    last_operation_id, created_at, updated_at
                ) VALUES (?, ?, ?, 0, NULL, ?, ?)
                """,
                (instance_id, thread, _json(DEFAULT_SOMATIC_STATE), now, now),
            )
            row = conn.execute(
                "SELECT * FROM somatic_states "
                "WHERE persona_instance_id = ? AND thread_id = ?",
                (instance_id, thread),
            ).fetchone()
    if row is None:
        return StoredSomaticState(
            persona_instance_id=instance_id,
            thread_id=thread,
            state=SomaticState(DEFAULT_SOMATIC_STATE),
            revision=0,
            last_operation_id=None,
            updated_at=now,
        )
    return _state_from_row(row)


def get_somatic_operation(operation_id: str) -> dict[str, Any] | None:
    ensure_somatic_store()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM somatic_operations WHERE operation_id = ?",
            (str(operation_id or "").strip(),),
        ).fetchone()
    if row is None:
        return None
    return {
        "operation_id": row["operation_id"],
        "persona_instance_id": row["persona_instance_id"],
        "thread_id": row["thread_id"],
        "source_event_id": row["source_event_id"],
        "source_kind": row["source_kind"],
        "state_before": _object(row["state_before_json"]),
        "state_after": _object(row["state_after_json"]),
        "decision": _object(row["decision_json"]),
        "appraisal": _object(row["appraisal_json"]),
        "result": _object(row["result_json"]),
        "resulting_revision": int(row["resulting_revision"]),
        "created_at": row["created_at"],
    }


def commit_somatic_transition(
    *,
    operation_id: str,
    persona_instance_id: str,
    thread_id: str,
    source_event_id: str,
    source_kind: str,
    expected_revision: int,
    state_after: SomaticState | Mapping[str, float],
    decision: Mapping[str, Any],
    appraisal: Mapping[str, Any],
    result: Mapping[str, Any],
) -> SomaticCommitResult:
    """原子提交一次已确认的身体变化。

    同一个 operation_id 重试时直接返回第一次结果。revision 不匹配时拒绝写入，
    由上层重新读取并重新评价，不能把旧状态上的计算硬套到新状态上。
    """
    instance_id, thread = _identity(persona_instance_id, thread_id)
    op_id = str(operation_id or "").strip()
    event_id = str(source_event_id or "").strip()
    kind = str(source_kind or "").strip()
    if not op_id or not event_id or not kind:
        raise ValueError("operation_id、source_event_id 和 source_kind 不能为空")
    if isinstance(expected_revision, bool) or int(expected_revision) < 0:
        raise ValueError("expected_revision 必须是非负整数")
    next_state = state_after if isinstance(state_after, SomaticState) else SomaticState(state_after)

    ensure_somatic_store()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM somatic_operations WHERE operation_id = ?",
            (op_id,),
        ).fetchone()
        if existing is not None:
            existing_op = {
                "operation_id": existing["operation_id"],
                "persona_instance_id": existing["persona_instance_id"],
                "thread_id": existing["thread_id"],
                "source_event_id": existing["source_event_id"],
                "source_kind": existing["source_kind"],
                "state_before": _object(existing["state_before_json"]),
                "state_after": _object(existing["state_after_json"]),
                "decision": _object(existing["decision_json"]),
                "appraisal": _object(existing["appraisal_json"]),
                "result": _object(existing["result_json"]),
                "resulting_revision": int(existing["resulting_revision"]),
                "created_at": existing["created_at"],
            }
            if existing_op["persona_instance_id"] != instance_id or existing_op["thread_id"] != thread:
                raise ValueError("operation_id 已被另一个身体实例使用")
            state_row = conn.execute(
                "SELECT * FROM somatic_states "
                "WHERE persona_instance_id = ? AND thread_id = ?",
                (instance_id, thread),
            ).fetchone()
            return SomaticCommitResult(
                status="committed",
                record=_state_from_row(state_row),
                operation=existing_op,
                duplicate=True,
            )

        state_row = conn.execute(
            "SELECT * FROM somatic_states "
            "WHERE persona_instance_id = ? AND thread_id = ?",
            (instance_id, thread),
        ).fetchone()
        if state_row is None:
            now = _now()
            conn.execute(
                """
                INSERT INTO somatic_states (
                    persona_instance_id, thread_id, state_json, revision,
                    last_operation_id, created_at, updated_at
                ) VALUES (?, ?, ?, 0, NULL, ?, ?)
                """,
                (instance_id, thread, _json(DEFAULT_SOMATIC_STATE), now, now),
            )
            state_row = conn.execute(
                "SELECT * FROM somatic_states "
                "WHERE persona_instance_id = ? AND thread_id = ?",
                (instance_id, thread),
            ).fetchone()

        current = _state_from_row(state_row)
        if current.revision != int(expected_revision):
            return SomaticCommitResult(
                status="revision_conflict",
                record=current,
                operation={},
            )

        new_revision = current.revision + 1
        now = _now()
        conn.execute(
            """
            UPDATE somatic_states
            SET state_json = ?, revision = ?, last_operation_id = ?, updated_at = ?
            WHERE persona_instance_id = ? AND thread_id = ? AND revision = ?
            """,
            (
                _json(next_state.to_dict()),
                new_revision,
                op_id,
                now,
                instance_id,
                thread,
                current.revision,
            ),
        )
        operation = {
            "operation_id": op_id,
            "persona_instance_id": instance_id,
            "thread_id": thread,
            "source_event_id": event_id,
            "source_kind": kind,
            "state_before": current.state.to_dict(),
            "state_after": next_state.to_dict(),
            "decision": dict(decision),
            "appraisal": dict(appraisal),
            "result": dict(result),
            "resulting_revision": new_revision,
            "created_at": now,
        }
        conn.execute(
            """
            INSERT INTO somatic_operations (
                operation_id, persona_instance_id, thread_id, source_event_id,
                source_kind, state_before_json, state_after_json, decision_json,
                appraisal_json, result_json, resulting_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                op_id,
                instance_id,
                thread,
                event_id,
                kind,
                _json(operation["state_before"]),
                _json(operation["state_after"]),
                _json(operation["decision"]),
                _json(operation["appraisal"]),
                _json(operation["result"]),
                new_revision,
                now,
            ),
        )
        updated_row = conn.execute(
            "SELECT * FROM somatic_states "
            "WHERE persona_instance_id = ? AND thread_id = ?",
            (instance_id, thread),
        ).fetchone()

    return SomaticCommitResult(
        status="committed",
        record=_state_from_row(updated_row),
        operation=operation,
    )


__all__ = [
    "DEFAULT_SOMATIC_STATE",
    "SomaticCommitResult",
    "StoredSomaticState",
    "commit_somatic_transition",
    "ensure_somatic_store",
    "get_somatic_operation",
    "get_somatic_state",
]
