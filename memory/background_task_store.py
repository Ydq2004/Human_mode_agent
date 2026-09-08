"""通用后台任务账本。

这是 Agent 主动发起的可选任务使用的框架层存储，不负责具体业务语义。
ExperienceAppraisal/CommitWorker 仍使用各自的专用账本；本模块只保存任务
生命周期、输入范围、结果和完成通知，保证后台线程或进程重启后可以恢复。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from config import SQLITE_DB_PATH


TASK_STATUSES = frozenset({
    "queued", "running", "cancel_requested", "retry_wait",
    "succeeded", "failed", "timeout", "cancelled", "abandoned", "exhausted",
})
TERMINAL_STATUSES = frozenset({
    "succeeded", "failed", "timeout", "cancelled", "abandoned", "exhausted",
})
_TRANSITIONS = {
    "queued": {"running", "cancel_requested", "failed", "abandoned"},
    "running": {"succeeded", "failed", "timeout", "cancel_requested", "retry_wait", "abandoned", "exhausted"},
    "cancel_requested": {"cancelled", "succeeded", "failed", "timeout"},
    "retry_wait": {"queued", "abandoned"},
}


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    Path(SQLITE_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=30)
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


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def ensure_background_task_store() -> None:
    """创建任务和通知表；不删除、覆盖已有数据。"""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS background_tasks (
                task_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                persona_instance_id TEXT,
                thread_id TEXT,
                source_event_id TEXT,
                idempotency_key TEXT UNIQUE,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                scope_json TEXT,
                result_json TEXT,
                result_schema_version INTEGER NOT NULL DEFAULT 1,
                error_code TEXT,
                error_message TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                queued_at TEXT,
                started_at TEXT,
                heartbeat_at TEXT,
                completed_at TEXT,
                next_retry_at TEXT,
                worker_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS background_task_events (
                event_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(task_id, event_type, created_at)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_background_tasks_status "
            "ON background_tasks(status, next_retry_at, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_background_tasks_scope "
            "ON background_tasks(persona_instance_id, thread_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_background_task_events_delivery "
            "ON background_task_events(delivered_at, created_at)"
        )


def create_task(
    *,
    task_type: str,
    execution_mode: str,
    request: dict[str, Any],
    scope: dict[str, Any] | None = None,
    persona_instance_id: str | None = None,
    thread_id: str | None = None,
    source_event_id: str | None = None,
    idempotency_key: str | None = None,
    max_attempts: int = 1,
    task_id: str | None = None,
) -> dict[str, Any]:
    if not str(task_type or "").strip():
        raise ValueError("task_type 不能为空")
    if execution_mode not in {"sync", "async"}:
        raise ValueError("execution_mode 必须是 sync 或 async")
    if not isinstance(request, dict):
        raise TypeError("request 必须是字典")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts 必须是正整数")

    ensure_background_task_store()
    now = _now()
    identifier = str(task_id or f"task_{uuid4().hex}").strip()
    key = str(idempotency_key or "").strip() or None
    with _connect() as conn:
        if key:
            existing = conn.execute(
                "SELECT * FROM background_tasks WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                return _decode_task(existing)
        conn.execute(
            """
            INSERT INTO background_tasks (
                task_id, task_type, execution_mode, persona_instance_id, thread_id,
                source_event_id, idempotency_key, status, request_json, scope_json,
                result_schema_version, attempt_count, max_attempts, created_at, queued_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, 1, 0, ?, ?, ?)
            """,
            (
                identifier, str(task_type).strip(), execution_mode,
                str(persona_instance_id or "").strip() or None,
                str(thread_id or "").strip() or None,
                str(source_event_id or "").strip() or None,
                key, _json(request), _json(scope), max_attempts, now, now,
            ),
        )
        row = conn.execute("SELECT * FROM background_tasks WHERE task_id = ?", (identifier,)).fetchone()
    return _decode_task(row)


def get_task(task_id: str, *, include_result: bool = True) -> dict[str, Any] | None:
    ensure_background_task_store()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM background_tasks WHERE task_id = ?", (str(task_id or "").strip(),)).fetchone()
    if row is None:
        return None
    item = _decode_task(row)
    if not include_result:
        item["result"] = None
    return item


def get_task_by_idempotency_key(key: str, *, include_result: bool = True) -> dict[str, Any] | None:
    """按幂等键读取已有任务，供工具层避免重复调度。"""
    value = str(key or "").strip()
    if not value:
        return None
    ensure_background_task_store()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM background_tasks WHERE idempotency_key = ?", (value,)).fetchone()
    if row is None:
        return None
    item = _decode_task(row)
    if not include_result:
        item["result"] = None
    return item


def list_tasks(
    *,
    persona_instance_id: str | None = None,
    thread_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit 必须在 1 到 200 之间")
    ensure_background_task_store()
    clauses: list[str] = []
    params: list[Any] = []
    if persona_instance_id:
        clauses.append("persona_instance_id = ?")
        params.append(str(persona_instance_id).strip())
    if thread_id:
        clauses.append("thread_id = ?")
        params.append(str(thread_id).strip())
    if status:
        if status not in TASK_STATUSES:
            raise ValueError("status 无效")
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM background_tasks " + where +
            " ORDER BY created_at DESC LIMIT ?", (*params, limit)
        ).fetchall()
    return [_decode_task(row) for row in rows]


def transition_task(
    task_id: str,
    target_status: str,
    *,
    expected_status: str | None = None,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    next_retry_at: str | None = None,
    worker_id: str | None = None,
) -> dict[str, Any] | None:
    if target_status not in TASK_STATUSES:
        raise ValueError("目标状态无效")
    ensure_background_task_store()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM background_tasks WHERE task_id = ?", (str(task_id or "").strip(),)).fetchone()
        if row is None:
            return None
        current = str(row["status"])
        if expected_status is not None and current != expected_status:
            return _decode_task(row)
        if current != target_status and target_status not in _TRANSITIONS.get(current, set()):
            raise ValueError(f"不允许的任务状态转换：{current} -> {target_status}")
        now = _now()
        started_at = row["started_at"]
        heartbeat_at = row["heartbeat_at"]
        completed_at = row["completed_at"]
        queued_at = row["queued_at"]
        attempt_count = int(row["attempt_count"] or 0)
        if target_status == "queued":
            queued_at = now
        if target_status == "running":
            started_at = started_at or now
            heartbeat_at = now
            attempt_count += 1
        if target_status in TERMINAL_STATUSES:
            completed_at = now
        conn.execute(
            """
            UPDATE background_tasks SET status=?, result_json=?, error_code=?, error_message=?,
                attempt_count=?, queued_at=?, started_at=?, heartbeat_at=?, completed_at=?,
                next_retry_at=?, worker_id=? WHERE task_id=?
            """,
            (
                target_status, _json(result), error_code, error_message, attempt_count,
                queued_at, started_at, heartbeat_at, completed_at, next_retry_at,
                worker_id, str(task_id or "").strip(),
            ),
        )
        updated = conn.execute("SELECT * FROM background_tasks WHERE task_id = ?", (str(task_id or "").strip(),)).fetchone()
    return _decode_task(updated)


def claim_task(task_id: str, *, worker_id: str) -> dict[str, Any] | None:
    ensure_background_task_store()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM background_tasks WHERE task_id = ?", (str(task_id or "").strip(),)).fetchone()
        if row is None or row["status"] not in {"queued", "retry_wait"}:
            return None
        if row["status"] == "retry_wait" and row["next_retry_at"]:
            try:
                if datetime.fromisoformat(row["next_retry_at"]) > datetime.now():
                    return None
            except ValueError:
                pass
        now = _now()
        conn.execute(
            "UPDATE background_tasks SET status='running', started_at=COALESCE(started_at, ?), heartbeat_at=?, attempt_count=attempt_count+1, worker_id=?, next_retry_at=NULL WHERE task_id=? AND status IN ('queued','retry_wait')",
            (now, now, worker_id, str(task_id or "").strip()),
        )
        claimed = conn.execute("SELECT * FROM background_tasks WHERE task_id = ?", (str(task_id or "").strip(),)).fetchone()
    return _decode_task(claimed) if claimed and claimed["status"] == "running" and claimed["worker_id"] == worker_id else None


def update_heartbeat(task_id: str, *, worker_id: str) -> bool:
    ensure_background_task_store()
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE background_tasks SET heartbeat_at=? WHERE task_id=? AND status='running' AND worker_id=?",
            (_now(), str(task_id or "").strip(), worker_id),
        )
    return cursor.rowcount > 0


def request_cancel(task_id: str) -> dict[str, Any] | None:
    ensure_background_task_store()
    with _connect() as conn:
        row = conn.execute("SELECT status FROM background_tasks WHERE task_id = ?", (str(task_id or "").strip(),)).fetchone()
        if row is None:
            return None
        status = str(row["status"])
        if status in TERMINAL_STATUSES:
            return get_task(task_id)
        if status == "queued":
            conn.execute("UPDATE background_tasks SET status='cancelled', completed_at=? WHERE task_id=?", (_now(), task_id))
        elif status in {"running", "retry_wait"}:
            conn.execute("UPDATE background_tasks SET status='cancel_requested' WHERE task_id=?", (task_id,))
        else:
            raise ValueError(f"当前任务不能取消：{status}")
    return get_task(task_id)


def recover_stale_tasks(*, max_age_minutes: float = 30.0) -> list[dict[str, Any]]:
    """重启时恢复失联任务；只处理状态，不执行具体任务。"""
    if max_age_minutes < 0:
        raise ValueError("max_age_minutes 必须非负")
    ensure_background_task_store()
    cutoff = datetime.now() - timedelta(minutes=float(max_age_minutes))
    changed: list[dict[str, Any]] = []
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM background_tasks WHERE status IN ('running','cancel_requested','retry_wait')").fetchall()
        for row in rows:
            reference = row["heartbeat_at"] or row["started_at"] or row["created_at"]
            try:
                stale = datetime.fromisoformat(str(reference)) < cutoff
            except ValueError:
                stale = True
            if not stale:
                continue
            if row["status"] == "cancel_requested":
                target = "cancelled"
            elif int(row["attempt_count"] or 0) < int(row["max_attempts"] or 1):
                target = "queued"
            else:
                target = "abandoned"
            conn.execute(
                "UPDATE background_tasks SET status=?, completed_at=?, worker_id=NULL, next_retry_at=NULL WHERE task_id=?",
                (target, _now() if target in TERMINAL_STATUSES else None, row["task_id"]),
            )
            changed.append({"task_id": row["task_id"], "from": row["status"], "to": target})
    return changed


def append_event(task_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not str(event_type or "").strip():
        raise ValueError("event_type 不能为空")
    ensure_background_task_store()
    event_id = f"task_event_{uuid4().hex}"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO background_task_events (event_id, task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_id, str(task_id or "").strip(), str(event_type).strip(), _json(payload) or "{}", _now()),
        )
        row = conn.execute("SELECT * FROM background_task_events WHERE event_id = ?", (event_id,)).fetchone()
    return _decode_event(row)


def list_pending_events(*, limit: int = 50) -> list[dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit 必须在 1 到 200 之间")
    ensure_background_task_store()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM background_task_events WHERE delivered_at IS NULL ORDER BY created_at LIMIT ?", (limit,)
        ).fetchall()
    return [_decode_event(row) for row in rows]


def get_event(event_id: str) -> dict[str, Any] | None:
    ensure_background_task_store()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM background_task_events WHERE event_id = ?",
            (str(event_id or "").strip(),),
        ).fetchone()
    return _decode_event(row) if row is not None else None


def mark_event_delivered(event_id: str) -> bool:
    ensure_background_task_store()
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE background_task_events SET delivered_at=?, delivery_attempts=delivery_attempts+1 WHERE event_id=? AND delivered_at IS NULL",
            (_now(), str(event_id or "").strip()),
        )
    return cursor.rowcount > 0


def _decode_task(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    item = dict(row)
    item["request"] = _decode(item.pop("request_json", None)) or {}
    item["scope"] = _decode(item.pop("scope_json", None)) or {}
    item["result"] = _decode(item.pop("result_json", None))
    return item


def _decode_event(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    item = dict(row)
    item["payload"] = _decode(item.pop("payload_json", None)) or {}
    return item


__all__ = [
    "TASK_STATUSES", "TERMINAL_STATUSES", "ensure_background_task_store",
    "create_task", "get_task", "list_tasks", "transition_task", "claim_task",
    "get_task_by_idempotency_key",
    "update_heartbeat", "request_cancel", "recover_stale_tasks", "append_event",
    "list_pending_events", "get_event", "mark_event_delivered",
]
