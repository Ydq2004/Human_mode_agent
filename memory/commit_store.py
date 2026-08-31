"""后台 appraisal/commit 状态账本。

ExperienceSlice 保存原始经历；本模块只保存后台任务的状态和结果，
用于重启后查看哪些任务已经评价、已经提交或提交失败。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
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
                appraisal_input_json TEXT,
                appraisal_json TEXT,
                effects_json TEXT,
                event_evidence_json TEXT,
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
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(commit_ledger)")
        }
        if "event_evidence_json" not in columns:
            conn.execute(
                "ALTER TABLE commit_ledger ADD COLUMN event_evidence_json TEXT"
            )
        if "appraisal_input_json" not in columns:
            conn.execute(
                "ALTER TABLE commit_ledger ADD COLUMN appraisal_input_json TEXT"
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
                    thread_id, appraisal_status, appraisal_input_json, appraisal_json, effects_json,
                    commit_status, commit_result_json, error, submitted_at,
                    appraisal_completed_at, commit_started_at, commit_completed_at
                )
                SELECT job_id, experience_slice_id, event_id, event_sequence,
                    thread_id, appraisal_status, NULL, appraisal_json, effects_json,
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


def record_appraisal_pending(
    *,
    job_id: str,
    experience_slice_id: str,
    event_id: str,
    event_sequence: int,
    thread_id: str,
    persona_context: dict[str, Any],
    mood_reactivity: float,
    submitted_at: str | None = None,
) -> None:
    """在提交后台评价前保存可重建输入；这是跨进程恢复的依据。"""
    if not job_id or not experience_slice_id or not event_id or not thread_id:
        raise ValueError("评价输入缺少任务、经历或线程标识")
    if not isinstance(event_sequence, int) or event_sequence < 1:
        raise ValueError("event_sequence 必须是正整数")
    ensure_commit_ledger()
    input_json = _json({
        "persona_context": persona_context,
        "mood_reactivity": float(mood_reactivity),
    })
    now = str(submitted_at or _now())
    with _connect() as conn:
        existing = conn.execute(
            "SELECT appraisal_input_json, appraisal_status FROM commit_ledger WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if existing is not None:
            if existing["appraisal_input_json"] not in {None, input_json}:
                raise ValueError(f"job_id {job_id} 的评价输入不一致")
            if existing["appraisal_input_json"] is None and existing["appraisal_status"] == "pending":
                conn.execute(
                    "UPDATE commit_ledger SET appraisal_input_json=? WHERE job_id=?",
                    (input_json, job_id),
                )
            return
        conn.execute(
            """
            INSERT INTO commit_ledger (
                job_id, experience_slice_id, event_id, event_sequence, thread_id,
                appraisal_status, appraisal_input_json, appraisal_json, effects_json,
                event_evidence_json, commit_status, commit_result_json, error,
                submitted_at, appraisal_completed_at, commit_started_at,
                commit_completed_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, NULL, 'waiting', NULL, NULL, ?, NULL, NULL, NULL)
            """,
            (job_id, experience_slice_id, event_id, event_sequence, thread_id, input_json, now),
        )


def list_recoverable_appraisals(
    *,
    thread_id: str | None = None,
    max_age_hours: float | None = 24.0,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """读取有完整输入、仍处于 pending 的 appraisal 任务。"""
    ensure_commit_ledger()
    clauses = [
        "appraisal_status = 'pending'",
        "commit_status = 'waiting'",
        "appraisal_input_json IS NOT NULL",
    ]
    params: list[Any] = []
    if thread_id:
        clauses.append("thread_id = ?")
        params.append(str(thread_id).strip())
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM commit_ledger WHERE " + " AND ".join(clauses)
            + " ORDER BY event_sequence",
            params,
        ).fetchall()
    cutoff = None
    if max_age_hours is not None:
        cutoff = (now or datetime.now()) - timedelta(hours=float(max_age_hours))
    output = []
    for row in rows:
        try:
            submitted = datetime.fromisoformat(str(row["submitted_at"]))
        except (TypeError, ValueError):
            continue
        if cutoff is not None and submitted < cutoff:
            continue
        item = _decode_ledger_row(row)
        try:
            item["appraisal_input"] = json.loads(row["appraisal_input_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        output.append(item)
    return output


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
            "SELECT appraisal_input_json, appraisal_json, effects_json, event_evidence_json, "
            "appraisal_status "
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
            _json(job.get("appraisal_input")),
            _json(job.get("appraisal")),
            _json(job.get("effects")),
            "waiting",
            _json(job.get("event_evidence")),
            None,
            job.get("error"),
            str(job.get("submitted_at") or _now()),
            str(job.get("completed_at") or _now()),
            None,
            None,
        )
        if existing is not None:
            if (
                existing["appraisal_status"] not in {"pending", status}
                or (
                    existing["appraisal_json"] is not None
                    and existing["appraisal_json"] != values[7]
                )
                or (
                    existing["effects_json"] is not None
                    and existing["effects_json"] != values[8]
                )
                or (
                    existing["event_evidence_json"] is not None
                    and existing["event_evidence_json"] != values[10]
                )
            ):
                raise ValueError(f"job_id {job['job_id']} 的评价结果不一致")
            if existing["appraisal_status"] == "pending":
                conn.execute(
                    "UPDATE commit_ledger SET appraisal_status=?, appraisal_json=?, "
                    "effects_json=?, event_evidence_json=?, error=?, appraisal_completed_at=? "
                    "WHERE job_id=?",
                    (
                        status,
                        values[7],
                        values[8],
                        values[10],
                        values[12],
                        values[14],
                        job["job_id"],
                    ),
                )
            return
            return

        conn.execute(
            """
            INSERT INTO commit_ledger (
                job_id, experience_slice_id, event_id, event_sequence, thread_id,
                appraisal_status, appraisal_input_json, appraisal_json, effects_json, commit_status,
                event_evidence_json, commit_result_json, error, submitted_at,
                appraisal_completed_at,
                commit_started_at, commit_completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )


def mark_commit_started(job_id: str) -> None:
    """
    账本标记开始提交
    """
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
    """账本标记提交进入终态"""
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
    """读取尚未进入提交终态的账本记录。"""
    ensure_commit_ledger()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM commit_ledger WHERE commit_status NOT IN "
            "('committed', 'commit_failed', 'abandoned') ORDER BY event_sequence"
        ).fetchall()
    return [_decode_ledger_row(row) for row in rows]


def list_commit_records(*, thread_id: str | None = None) -> list[dict[str, Any]]:
    """读取指定线程的完整账本，供启动恢复判断事件序号状态。"""
    ensure_commit_ledger()
    clauses = []
    params: list[Any] = []
    if thread_id is not None and str(thread_id).strip():
        clauses.append("thread_id = ?")
        params.append(str(thread_id).strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM commit_ledger "
            f"{where} ORDER BY event_sequence, submitted_at, job_id",
            params,
        ).fetchall()
    return [_decode_ledger_row(row) for row in rows]


def _decode_ledger_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """把账本 JSON 字段恢复成 Python 对象，供恢复工具和调度器使用。"""
    item = dict(row)
    for key in (
        "appraisal_input_json",
        "appraisal_json",
        "effects_json",
        "event_evidence_json",
        "commit_result_json",
    ):
        raw = item.get(key)
        if raw is None:
            item[key] = None
            continue
        try:
            item[key] = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            item[key] = None
    return item


def list_recoverable_commits(
    *,
    thread_id: str | None = None,
    max_age_hours: float | None = 24.0,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """读取可以进入人工/自动恢复评估的提交任务。

    这里只返回“评价已完成、提交仍等待”的记录；真正的 pending appraisal
    目前没有完整的持久输入，不能被本函数伪装成可恢复任务。超过保留时间
    的记录也不自动重跑，调用方可以用 ``mark_commit_abandoned`` 收口。
    """
    if max_age_hours is not None and (
        isinstance(max_age_hours, bool)
        or not isinstance(max_age_hours, (int, float))
        or max_age_hours < 0
    ):
        raise ValueError("max_age_hours 必须是非负数字或 None")

    ensure_commit_ledger()
    clauses = [
        "appraisal_status = ?",
        "commit_status = ?",
    ]
    params: list[Any] = ["completed", "waiting"]
    if thread_id is not None and str(thread_id).strip():
        clauses.append("thread_id = ?")
        params.append(str(thread_id).strip())

    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM commit_ledger WHERE "
            + " AND ".join(clauses)
            + " ORDER BY event_sequence, submitted_at, job_id",
            params,
        ).fetchall()

    decoded = [_decode_ledger_row(row) for row in rows]
    cutoff = None
    if max_age_hours is not None:
        reference = now or datetime.now()
        cutoff = reference - timedelta(hours=float(max_age_hours))
    output = []
    # 同一事件序号只能有一个自动恢复候选；如果该序号已经有终态记录，
    # 说明它可能是旧进程重复留下的记录，不能再次写入。
    all_records = list_commit_records(thread_id=thread_id)
    terminal_sequences = {
        int(item["event_sequence"])
        for item in all_records
        if item.get("commit_status") in {"committed", "commit_failed", "abandoned"}
    }
    seen_sequences: set[int] = set()
    for item in decoded:
        sequence = item.get("event_sequence")
        if not isinstance(sequence, int) or sequence in terminal_sequences:
            continue
        if sequence in seen_sequences:
            continue
        if not isinstance(item.get("appraisal_json"), dict):
            continue
        if not isinstance(item.get("effects_json"), dict):
            continue
        try:
            submitted_at = datetime.fromisoformat(str(item["submitted_at"]))
        except (KeyError, TypeError, ValueError):
            # 时间损坏时不自动恢复，交给人工处理，避免错误任务被误重放。
            continue
        if cutoff is None or submitted_at >= cutoff:
            seen_sequences.add(sequence)
            output.append(item)
    return output


def mark_commit_abandoned(job_id: str, reason: str) -> bool:
    """明确关闭无法安全恢复的任务，保留原因，不删除账本记录。"""
    job_id = str(job_id or "").strip()
    reason = str(reason or "").strip()
    if not job_id or not reason:
        raise ValueError("job_id 和 reason 不能为空")

    ensure_commit_ledger()
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE commit_ledger SET commit_status = ?, error = ?, "
            "commit_completed_at = ? WHERE job_id = ? AND commit_status "
            "NOT IN ('committed', 'commit_failed', 'abandoned')",
            ("abandoned", reason, _now(), job_id),
        )
    return cursor.rowcount > 0
