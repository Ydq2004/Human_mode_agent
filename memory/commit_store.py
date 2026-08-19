"""后台 appraisal/commit 状态账本。

ExperienceSlice 保存原始经历；本模块只保存后台任务的状态和结果，
用于重启后查看哪些任务已经评价、已经提交或提交失败。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from config import SQLITE_DB_PATH


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


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ensure_commit_ledger() -> None:
    """创建任务账本，不删除已有记录。"""
    with _connect() as conn:
        existing_schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'commit_ledger'"
        ).fetchone()
        # 早期开发版本误把 event_sequence 设成了全库唯一。
        # 它实际上只在一次运行、一个线程内负责排序；这里做一次保留数据的
        # 表迁移，避免重启后再次从 1 开始时被旧唯一约束拦住。
        if (
            existing_schema is not None
            and "event_sequence INTEGER NOT NULL UNIQUE"
            in str(existing_schema[0])
        ):
            conn.execute("DROP INDEX IF EXISTS idx_commit_ledger_status")
            conn.execute(
                "ALTER TABLE commit_ledger RENAME TO commit_ledger_legacy"
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS commit_ledger (
                job_id TEXT PRIMARY KEY,
                experience_slice_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_sequence INTEGER NOT NULL,
                thread_id TEXT NOT NULL,
                appraisal_status TEXT NOT NULL,
                appraisal_json TEXT,
                effects_json TEXT,
                commit_status TEXT NOT NULL,
                commit_result_json TEXT,
                error TEXT,
                submitted_at TEXT NOT NULL,
                appraisal_completed_at TEXT,
                commit_started_at TEXT,
                commit_completed_at TEXT
            )
            """
        )
        if existing_schema is not None and "commit_ledger_legacy" in {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }:
            conn.execute(
                """
                INSERT OR IGNORE INTO commit_ledger (
                    job_id, experience_slice_id, event_id, event_sequence,
                    thread_id, appraisal_status, appraisal_json, effects_json,
                    commit_status, commit_result_json, error, submitted_at,
                    appraisal_completed_at, commit_started_at, commit_completed_at
                )
                SELECT job_id, experience_slice_id, event_id, event_sequence,
                    thread_id, appraisal_status, appraisal_json, effects_json,
                    commit_status, commit_result_json, error, submitted_at,
                    appraisal_completed_at, commit_started_at, commit_completed_at
                FROM commit_ledger_legacy
                """
            )
            conn.execute("DROP TABLE commit_ledger_legacy")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_commit_ledger_status "
            "ON commit_ledger (commit_status, event_sequence)"
        )


def record_appraisal_terminal(job: dict[str, Any]) -> None:
    """评价进入 completed/failed 后先写入账本。"""
    status = str(job.get("status") or "failed")
    sequence = job.get("event_sequence")
    thread_id = str(job.get("thread_id") or "").strip()
    if not job.get("job_id") or not job.get("experience_slice_id"):
        raise ValueError("评价任务缺少 job_id 或 experience_slice_id")
    if not isinstance(sequence, int) or sequence < 1 or not thread_id:
        raise ValueError("评价任务缺少有效 event_sequence 或 thread_id")

    ensure_commit_ledger()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT appraisal_json, effects_json, appraisal_status "
            "FROM commit_ledger WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()
        values = (
            job["job_id"],
            job["experience_slice_id"],
            str(job.get("event_id") or ""),
            sequence,
            thread_id,
            status,
            _json(job.get("appraisal")),
            _json(job.get("effects")),
            "waiting",
            None,
            job.get("error"),
            str(job.get("submitted_at") or _now()),
            str(job.get("completed_at") or _now()),
            None,
            None,
        )
        if existing is not None:
            if (
                existing["appraisal_json"] != values[6]
                or existing["effects_json"] != values[7]
                or existing["appraisal_status"] != status
            ):
                raise ValueError(f"job_id {job['job_id']} 的评价结果不一致")
            return

        conn.execute(
            """
            INSERT INTO commit_ledger (
                job_id, experience_slice_id, event_id, event_sequence, thread_id,
                appraisal_status, appraisal_json, effects_json, commit_status,
                commit_result_json, error, submitted_at, appraisal_completed_at,
                commit_started_at, commit_completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )


def mark_commit_started(job_id: str) -> None:
    ensure_commit_ledger()
    with _connect() as conn:
        conn.execute(
            "UPDATE commit_ledger SET commit_status = ?, commit_started_at = ? "
            "WHERE job_id = ?",
            ("committing", _now(), job_id),
        )


def mark_commit_terminal(
    job_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    if status not in {"committed", "commit_failed"}:
        raise ValueError("无效的提交终态")
    ensure_commit_ledger()
    with _connect() as conn:
        conn.execute(
            "UPDATE commit_ledger SET commit_status = ?, "
            "commit_result_json = ?, error = ?, commit_completed_at = ? "
            "WHERE job_id = ?",
            (status, _json(result), error, _now(), job_id),
        )


def list_unfinished_commits() -> list[dict[str, Any]]:
    """读取尚未进入提交终态的账本记录；本阶段只查看，不自动重跑。"""
    ensure_commit_ledger()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM commit_ledger WHERE commit_status NOT IN "
            "('committed', 'commit_failed') ORDER BY event_sequence"
        ).fetchall()
    return [dict(row) for row in rows]
