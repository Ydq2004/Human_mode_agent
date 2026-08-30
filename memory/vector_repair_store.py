"""SQLite 中的 Chroma 修复队列。

SQLite 是认知事实的权威来源，Chroma 只是为了快速检索的索引。
因此两者同步失败时，不能回滚或伪装成成功，而要把“待修复”状态留下来。
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from config import (
    SQLITE_DB_PATH,
    VECTOR_REPAIR_MAX_ATTEMPTS,
    VECTOR_REPAIR_RETRY_DELAY_SECONDS,
)


PENDING = "pending"
REPAIRING = "repairing"
REPAIRED = "repaired"
FAILED = "failed"
EXHAUSTED = "exhausted"


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """打开修复队列连接，并确保事务结束后关闭。"""
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


def ensure_table() -> None:
    """创建队列表；不会删除已有任务。"""
    Path(SQLITE_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vector_repair_queue (
                repair_id TEXT PRIMARY KEY,
                concept_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                entity_snapshot_json TEXT,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_attempt_at TEXT,
                next_retry_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(vector_repair_queue)")
        }
        if "last_attempt_at" not in columns:
            conn.execute(
                "ALTER TABLE vector_repair_queue ADD COLUMN last_attempt_at TEXT"
            )
        if "next_retry_at" not in columns:
            conn.execute(
                "ALTER TABLE vector_repair_queue ADD COLUMN next_retry_at TEXT"
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vector_repair_queue_status
            ON vector_repair_queue(status, updated_at)
            """
        )


def _repair_id(concept_id: str, operation: str) -> str:
    raw = f"{concept_id}|{operation}".encode("utf-8")
    return "vector_repair_" + hashlib.sha256(raw).hexdigest()[:24]


def enqueue(
    concept_id: str,
    operation: str,
    *,
    entity_snapshot: dict[str, Any] | None = None,
    error: str | None = None,
) -> str:
    """记录一次索引同步失败；同一实体和操作只保留一条待修复任务。"""
    concept_id = str(concept_id or "").strip()
    operation = str(operation or "").strip()
    if not concept_id or not operation:
        raise ValueError("concept_id 和 operation 不能为空")

    ensure_table()
    repair_id = _repair_id(concept_id, operation)
    now = _now()
    snapshot_json = (
        json.dumps(entity_snapshot, ensure_ascii=False, sort_keys=True)
        if entity_snapshot is not None
        else None
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO vector_repair_queue (
                repair_id, concept_id, operation, entity_snapshot_json,
                status, attempt_count, last_error, last_attempt_at, next_retry_at,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, NULL, NULL, ?, ?)
            ON CONFLICT(repair_id) DO UPDATE SET
                entity_snapshot_json=excluded.entity_snapshot_json,
                status=? ,
                last_error=excluded.last_error,
                next_retry_at=NULL,
                updated_at=excluded.updated_at
            """,
            (
                repair_id,
                concept_id,
                operation,
                snapshot_json,
                PENDING,
                error,
                now,
                now,
                PENDING,
            ),
        )
    return repair_id


def list_pending(
    limit: int | None = None,
    *,
    include_exhausted: bool = False,
) -> list[dict[str, Any]]:
    """读取待处理任务；可选包含达到重试上限的任务供人工查看。"""
    ensure_table()
    statuses = [PENDING, FAILED, REPAIRING]
    if include_exhausted:
        statuses.append(EXHAUSTED)
    query = (
        "SELECT * FROM vector_repair_queue "
        f"WHERE status IN ({', '.join('?' for _ in statuses)}) "
        "ORDER BY updated_at, repair_id"
    )
    # REPAIRING 也要读出来：如果程序正好在修复中崩溃，重启后不能把任务
    # 永久留在“正在修复”状态。
    params: list[Any] = statuses
    if limit is not None:
        limit = max(0, int(limit))
        query += " LIMIT ?"
        params.append(limit)

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()

    output = []
    for row in rows:
        item = dict(row)
        raw_snapshot = item.pop("entity_snapshot_json")
        try:
            item["entity_snapshot"] = (
                json.loads(raw_snapshot) if raw_snapshot else None
            )
        except json.JSONDecodeError:
            item["entity_snapshot"] = None
        output.append(item)
    return output


def mark_attempt(repair_id: str) -> bool:
    """把任务标成正在修复，并把尝试次数加一。"""
    ensure_table()
    now = _now()
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE vector_repair_queue
            SET status=?, attempt_count=attempt_count + 1,
                last_attempt_at=?, updated_at=?
            WHERE repair_id=? AND status IN (?, ?, ?)
            """,
            (REPAIRING, now, now, repair_id, PENDING, FAILED, REPAIRING),
        )
    return cursor.rowcount > 0


def mark_repaired(repair_id: str) -> bool:
    """修复成功后关闭任务。"""
    ensure_table()
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE vector_repair_queue
            SET status=?, last_error=NULL, next_retry_at=NULL, updated_at=?
            WHERE repair_id=?
            """,
            (REPAIRED, _now(), repair_id),
        )
    return cursor.rowcount > 0


def mark_failed(
    repair_id: str,
    error: str,
    *,
    retry_delay_seconds: float = VECTOR_REPAIR_RETRY_DELAY_SECONDS,
    max_attempts: int = VECTOR_REPAIR_MAX_ATTEMPTS,
) -> bool:
    """记录失败；达到上限后停止自动重试，否则安排下一次时间。"""
    if (
        isinstance(retry_delay_seconds, bool)
        or not isinstance(retry_delay_seconds, (int, float))
        or retry_delay_seconds < 0
    ):
        raise ValueError("retry_delay_seconds 必须是非负数字")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts < 1
    ):
        raise ValueError("max_attempts 必须是正整数")
    now = datetime.now()
    next_retry = now + timedelta(seconds=float(retry_delay_seconds))
    ensure_table()
    with _connect() as conn:
        row = conn.execute(
            "SELECT attempt_count FROM vector_repair_queue WHERE repair_id=?",
            (repair_id,),
        ).fetchone()
        if row is None:
            return False
        status = (
            EXHAUSTED
            if int(row["attempt_count"] or 0) >= max_attempts
            else FAILED
        )
        cursor = conn.execute(
            """
            UPDATE vector_repair_queue
            SET status=?, last_error=?, next_retry_at=?, updated_at=?
            WHERE repair_id=?
            """,
            (
                status,
                str(error),
                None if status == EXHAUSTED else next_retry.isoformat(),
                now.isoformat(),
                repair_id,
            ),
        )
    return cursor.rowcount > 0


def is_retry_due(task: dict[str, Any], *, now: datetime | None = None) -> bool:
    """判断失败任务是否到达下一次重试时间；崩溃遗留的 repairing 可立即接管。"""
    if task.get("status") in {PENDING, REPAIRING}:
        return True
    if task.get("status") != FAILED:
        return False
    retry_at = task.get("next_retry_at")
    if not retry_at:
        return True
    try:
        return datetime.fromisoformat(str(retry_at)) <= (now or datetime.now())
    except ValueError:
        return False
