"""情景摘要的追加式 SQLite 存储。

摘要是 ExperienceSlice 的派生索引，不是原始经历，也不是 MemoryEntity。
本模块只负责保存和读取已经生成的摘要，不负责调用 LLM 或判断摘要内容。
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


SUMMARY_TYPES = frozenset({
    "segment",
    "daily",
    "weekly",
    "monthly",
    "annual",
})


@dataclass(frozen=True)
class ExperienceSummary:
    """一份可追溯、不可覆盖的情景摘要。"""

    summary_id: str
    summary_type: str
    start_at: str
    end_at: str
    source_slice_ids: tuple[str, ...]
    summary_text: str
    uncertainties: tuple[str, ...]
    algorithm_version: str
    prompt_version: str | None
    generated_at: str

    def __post_init__(self) -> None:
        summary_id = str(self.summary_id or "").strip()
        summary_type = str(self.summary_type or "").strip()
        start_at = str(self.start_at or "").strip()
        end_at = str(self.end_at or "").strip()
        summary_text = str(self.summary_text or "").strip()
        algorithm_version = str(self.algorithm_version or "").strip()

        if not summary_id:
            raise ValueError("summary_id 不能为空")
        if summary_type not in SUMMARY_TYPES:
            raise ValueError(f"不支持的 summary_type：{summary_type}")
        if not start_at or not end_at:
            raise ValueError("摘要必须有 start_at 和 end_at")
        if not summary_text:
            raise ValueError("summary_text 不能为空")
        if not algorithm_version:
            raise ValueError("algorithm_version 不能为空")

        source_ids = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in (self.source_slice_ids or ())
                if str(item or "").strip()
            )
        )
        if not source_ids:
            raise ValueError("摘要至少要引用一个 ExperienceSlice")

        uncertainty_items = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in (self.uncertainties or ())
                if str(item or "").strip()
            )
        )

        object.__setattr__(self, "summary_id", summary_id)
        object.__setattr__(self, "summary_type", summary_type)
        object.__setattr__(self, "start_at", start_at)
        object.__setattr__(self, "end_at", end_at)
        object.__setattr__(self, "source_slice_ids", source_ids)
        object.__setattr__(self, "summary_text", summary_text)
        object.__setattr__(self, "uncertainties", uncertainty_items)
        object.__setattr__(self, "algorithm_version", algorithm_version)
        object.__setattr__(
            self,
            "prompt_version",
            str(self.prompt_version).strip() if self.prompt_version else None,
        )
        object.__setattr__(self, "generated_at", str(self.generated_at or _now()))


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
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(summary: ExperienceSummary) -> str:
    material = {
        "summary_id": summary.summary_id,
        "summary_type": summary.summary_type,
        "start_at": summary.start_at,
        "end_at": summary.end_at,
        "source_slice_ids": summary.source_slice_ids,
        "summary_text": summary.summary_text,
        "uncertainties": summary.uncertainties,
        "algorithm_version": summary.algorithm_version,
        "prompt_version": summary.prompt_version,
    }
    return hashlib.sha256(_json(material).encode("utf-8")).hexdigest()


def _same_summary_content(
    left: ExperienceSummary,
    right: ExperienceSummary,
) -> bool:
    """比较摘要正文和来源，不把生成时间当作内容变化。"""
    return (
        left.summary_id == right.summary_id
        and left.summary_type == right.summary_type
        and left.start_at == right.start_at
        and left.end_at == right.end_at
        and left.source_slice_ids == right.source_slice_ids
        and left.summary_text == right.summary_text
        and left.uncertainties == right.uncertainties
        and left.algorithm_version == right.algorithm_version
        and left.prompt_version == right.prompt_version
    )


def ensure_summary_store() -> None:
    """创建摘要表和查询索引，不覆盖已有原始经历。"""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS experience_summaries (
                summary_id TEXT PRIMARY KEY,
                summary_type TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                source_slice_ids_json TEXT NOT NULL,
                summary_text TEXT NOT NULL,
                uncertainties_json TEXT NOT NULL,
                algorithm_version TEXT NOT NULL,
                prompt_version TEXT,
                generated_at TEXT NOT NULL,
                content_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_experience_summaries_period "
            "ON experience_summaries (summary_type, start_at, end_at)"
        )


def save_summary(summary: ExperienceSummary) -> bool:
    """追加保存摘要；相同内容重复保存返回 False，不同内容拒绝覆盖。"""
    if not isinstance(summary, ExperienceSummary):
        raise TypeError("summary 必须是 ExperienceSummary")

    ensure_summary_store()
    content_hash = _hash(summary)
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM experience_summaries WHERE summary_id = ?",
            (summary.summary_id,),
        ).fetchone()
        if existing is not None:
            # 通过完整字段比较兼容早期已经把 generated_at 放进指纹的记录。
            if not _same_summary_content(summary, _row_to_summary(existing)):
                raise ValueError(
                    f"summary_id {summary.summary_id} 已存在，但内容不同"
                )
            return False

        conn.execute(
            """
            INSERT INTO experience_summaries (
                summary_id, summary_type, start_at, end_at,
                source_slice_ids_json, summary_text, uncertainties_json,
                algorithm_version, prompt_version, generated_at, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                summary.summary_id,
                summary.summary_type,
                summary.start_at,
                summary.end_at,
                _json(summary.source_slice_ids),
                summary.summary_text,
                _json(summary.uncertainties),
                summary.algorithm_version,
                summary.prompt_version,
                summary.generated_at,
                content_hash,
            ),
        )
    return True


def _row_to_summary(row: sqlite3.Row) -> ExperienceSummary:
    return ExperienceSummary(
        summary_id=row["summary_id"],
        summary_type=row["summary_type"],
        start_at=row["start_at"],
        end_at=row["end_at"],
        source_slice_ids=tuple(json.loads(row["source_slice_ids_json"])),
        summary_text=row["summary_text"],
        uncertainties=tuple(json.loads(row["uncertainties_json"])),
        algorithm_version=row["algorithm_version"],
        prompt_version=row["prompt_version"],
        generated_at=row["generated_at"],
    )


def get_summary(summary_id: str) -> ExperienceSummary | None:
    ensure_summary_store()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM experience_summaries WHERE summary_id = ?",
            (str(summary_id or "").strip(),),
        ).fetchone()
    return _row_to_summary(row) if row else None


def list_summaries(
    *,
    summary_type: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int = 100,
) -> list[ExperienceSummary]:
    """按时间范围和摘要类型读取摘要，结果按覆盖开始时间排序。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit 必须是大于等于 1 的整数")
    if summary_type is not None and summary_type not in SUMMARY_TYPES:
        raise ValueError(f"不支持的 summary_type：{summary_type}")

    clauses = []
    params: list[Any] = []
    if summary_type is not None:
        clauses.append("summary_type = ?")
        params.append(summary_type)
    if start_at is not None:
        clauses.append("end_at >= ?")
        params.append(str(start_at))
    if end_at is not None:
        clauses.append("start_at <= ?")
        params.append(str(end_at))

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    ensure_summary_store()
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM experience_summaries {where} "
            "ORDER BY start_at ASC, summary_id ASC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [_row_to_summary(row) for row in rows]
