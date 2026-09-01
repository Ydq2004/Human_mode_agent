"""人格演化的持久化地基。

本模块只保存两类数据：实例第一次启动时的出生人格快照，以及从多段经历
整理出的追加式人格证据。它不调用 LLM，也不直接计算或修改当前人格；这样
后续的反思器和快照聚合器可以在明确边界下演进，而不会覆盖原始证据。
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

from config import SQLITE_DB_PATH


@dataclass(frozen=True)
class TraitEvidence:
    """一条可追溯的人格证据，不代表最终人格分数。"""

    evidence_id: str
    trait_id: str
    direction: str
    strength: float
    scope: str
    behavior_source: str
    evidence_slice_ids: tuple[str, ...]
    counter_evidence_slice_ids: tuple[str, ...]
    confidence: float
    dedupe_key: str
    algorithm_version: str
    created_at: str
    uncertainty: str = ""
    persona_instance_id: str = "default_instance"
    status: str = "accepted"

    def __post_init__(self) -> None:
        for name in (
            "evidence_id",
            "trait_id",
            "direction",
            "scope",
            "behavior_source",
            "dedupe_key",
            "algorithm_version",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} 不能为空")
        if not self.evidence_slice_ids:
            raise ValueError("人格证据至少要引用一条 ExperienceSlice")
        if any(not str(item).strip() for item in self.evidence_slice_ids):
            raise ValueError("evidence_slice_ids 不能包含空值")
        if any(not str(item).strip() for item in self.counter_evidence_slice_ids):
            raise ValueError("counter_evidence_slice_ids 不能包含空值")
        if not 0.0 <= float(self.strength) <= 1.0:
            raise ValueError("strength 必须在 0~1 之间")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence 必须在 0~1 之间")
        if self.status not in {"accepted", "rejected"}:
            raise ValueError("status 必须是 accepted 或 rejected")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
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
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def ensure_personality_store() -> None:
    """创建人格证据表；已有实例只补表，不删除任何数据。"""
    from pathlib import Path

    Path(SQLITE_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS personality_seed (
                seed_id TEXT PRIMARY KEY,
                persona_instance_id TEXT NOT NULL DEFAULT 'default_instance',
                persona_name TEXT NOT NULL,
                seed_json TEXT NOT NULL,
                source_persona_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trait_evidence (
                evidence_id TEXT PRIMARY KEY,
                persona_instance_id TEXT NOT NULL DEFAULT 'default_instance',
                trait_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                strength REAL NOT NULL,
                scope TEXT NOT NULL,
                behavior_source TEXT NOT NULL,
                evidence_slice_ids_json TEXT NOT NULL,
                counter_evidence_slice_ids_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'accepted',
                dedupe_key TEXT NOT NULL UNIQUE,
                algorithm_version TEXT NOT NULL,
                uncertainty TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        seed_columns = {row["name"] for row in conn.execute("PRAGMA table_info(personality_seed)")}
        if "persona_instance_id" not in seed_columns:
            conn.execute("ALTER TABLE personality_seed ADD COLUMN persona_instance_id TEXT NOT NULL DEFAULT 'default_instance'")
        evidence_columns = {row["name"] for row in conn.execute("PRAGMA table_info(trait_evidence)")}
        if "persona_instance_id" not in evidence_columns:
            conn.execute("ALTER TABLE trait_evidence ADD COLUMN persona_instance_id TEXT NOT NULL DEFAULT 'default_instance'")
        if "status" not in evidence_columns:
            conn.execute("ALTER TABLE trait_evidence ADD COLUMN status TEXT NOT NULL DEFAULT 'accepted'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trait_evidence_trait "
            "ON trait_evidence (trait_id, created_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS personality_reflection_runs (
                reflection_id TEXT PRIMARY KEY,
                persona_instance_id TEXT NOT NULL,
                last_analyzed_sequence INTEGER,
                this_run_start_sequence INTEGER,
                this_run_end_sequence INTEGER,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                failure_reason TEXT,
                algorithm_version TEXT NOT NULL,
                accepted_evidence_count INTEGER NOT NULL DEFAULT 0,
                rejected_evidence_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )


def save_personality_seed(
    *,
    seed_id: str,
    persona_name: str,
    seed: dict[str, Any],
    source_persona_hash: str,
    persona_instance_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """只写入一次出生人格；同一 seed_id 后续不会被角色卡静默覆盖。"""
    instance_id = str(persona_instance_id or seed_id).strip()
    if not str(seed_id or "").strip() or not str(persona_name or "").strip() or not instance_id:
        raise ValueError("seed_id 和 persona_name 不能为空")
    if not isinstance(seed, dict):
        raise TypeError("seed 必须是字典")
    if not str(source_persona_hash or "").strip():
        raise ValueError("source_persona_hash 不能为空")

    ensure_personality_store()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM personality_seed WHERE seed_id = ?",
            (str(seed_id).strip(),),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        conn.execute(
            """
            INSERT INTO personality_seed
                (seed_id, persona_instance_id, persona_name, seed_json, source_persona_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(seed_id).strip(),
                instance_id,
                str(persona_name).strip(),
                _json(seed),
                str(source_persona_hash).strip(),
                str(created_at or _now()),
            ),
        )
        row = conn.execute(
            "SELECT * FROM personality_seed WHERE seed_id = ?",
            (str(seed_id).strip(),),
        ).fetchone()
    return dict(row)


def ensure_personality_seed_from_persona(
    persona: dict[str, Any],
    *,
    persona_instance_id: str | None = None,
) -> dict[str, Any]:
    """首次启动时从角色卡复制出生人格；已有 seed 永不覆盖。"""
    if not isinstance(persona, dict):
        raise TypeError("persona 必须是字典")
    agent_name = str(persona.get("agent_name") or "助理").strip()
    instance_id = str(persona_instance_id or persona.get("persona_instance_id") or agent_name).strip()
    if not instance_id:
        raise ValueError("persona_instance_id 不能为空")
    explicit_traits = persona.get("personality_traits_seed") or {}
    if not isinstance(explicit_traits, dict):
        explicit_traits = {}
    seed = {
        "personality": str(persona.get("personality") or "").strip(),
        "traits": dict(explicit_traits),
    }
    source_hash = hashlib.sha256(
        json.dumps(persona, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return save_personality_seed(
        seed_id=instance_id,
        persona_instance_id=instance_id,
        persona_name=agent_name,
        seed=seed,
        source_persona_hash=source_hash,
    )


def get_personality_seed(seed_id: str) -> dict[str, Any] | None:
    ensure_personality_store()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM personality_seed WHERE seed_id = ?",
            (str(seed_id or "").strip(),),
        ).fetchone()
    return dict(row) if row else None


def _row_to_evidence(row: sqlite3.Row) -> TraitEvidence:
    def load_list(key: str) -> tuple[str, ...]:
        try:
            value = json.loads(row[key])
        except (TypeError, json.JSONDecodeError):
            value = []
        return tuple(str(item) for item in value) if isinstance(value, list) else ()

    return TraitEvidence(
        evidence_id=row["evidence_id"],
        trait_id=row["trait_id"],
        direction=row["direction"],
        strength=float(row["strength"]),
        scope=row["scope"],
        behavior_source=row["behavior_source"],
        evidence_slice_ids=load_list("evidence_slice_ids_json"),
        counter_evidence_slice_ids=load_list("counter_evidence_slice_ids_json"),
        confidence=float(row["confidence"]),
        dedupe_key=row["dedupe_key"],
        algorithm_version=row["algorithm_version"],
        uncertainty=row["uncertainty"],
        created_at=row["created_at"],
        persona_instance_id=row["persona_instance_id"] if "persona_instance_id" in row.keys() else "default_instance",
        status=row["status"] if "status" in row.keys() else "accepted",
    )


def append_trait_evidence(evidence: TraitEvidence) -> TraitEvidence:
    """追加一条证据；同一 dedupe_key 重复提交时返回原记录。"""
    if not isinstance(evidence, TraitEvidence):
        raise TypeError("evidence 必须是 TraitEvidence")
    ensure_personality_store()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM trait_evidence WHERE dedupe_key = ?",
            (evidence.dedupe_key,),
        ).fetchone()
        if existing is not None:
            return _row_to_evidence(existing)
        conn.execute(
            """
            INSERT INTO trait_evidence (
                evidence_id, persona_instance_id, trait_id, direction, strength, scope,
                behavior_source, evidence_slice_ids_json,
                counter_evidence_slice_ids_json, confidence, dedupe_key,
                status, algorithm_version, uncertainty, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_id,
                evidence.persona_instance_id,
                evidence.trait_id,
                evidence.direction,
                float(evidence.strength),
                evidence.scope,
                evidence.behavior_source,
                _json(list(evidence.evidence_slice_ids)),
                _json(list(evidence.counter_evidence_slice_ids)),
                float(evidence.confidence),
                evidence.dedupe_key,
                evidence.status,
                evidence.algorithm_version,
                evidence.uncertainty,
                evidence.created_at or _now(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM trait_evidence WHERE evidence_id = ?",
            (evidence.evidence_id,),
        ).fetchone()
    return _row_to_evidence(row)


def list_trait_evidence(
    *,
    persona_instance_id: str | None = None,
    trait_id: str | None = None,
    status: str | None = None,
    as_of_timestamp: str | None = None,
    limit: int = 1000,
) -> list[TraitEvidence]:
    """读取追加式证据，供后续反思器和快照聚合器使用。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit 必须是大于等于 1 的整数")
    ensure_personality_store()
    clauses = []
    params: list[Any] = []
    if trait_id is not None and str(trait_id).strip():
        clauses.append("trait_id = ?")
        params.append(str(trait_id).strip())
    if persona_instance_id is not None and str(persona_instance_id).strip():
        clauses.append("persona_instance_id = ?")
        params.append(str(persona_instance_id).strip())
    if status is not None and str(status).strip():
        clauses.append("status = ?")
        params.append(str(status).strip())
    if as_of_timestamp is not None and str(as_of_timestamp).strip():
        clauses.append("created_at <= ?")
        params.append(str(as_of_timestamp).strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trait_evidence "
            f"{where} ORDER BY created_at ASC, evidence_id ASC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [_row_to_evidence(row) for row in rows]


def record_reflection_run(
    *,
    reflection_id: str,
    persona_instance_id: str,
    status: str,
    last_analyzed_sequence: int | None = None,
    this_run_start_sequence: int | None = None,
    this_run_end_sequence: int | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    failure_reason: str | None = None,
    algorithm_version: str = "personality-reflection-v1",
    accepted_evidence_count: int = 0,
    rejected_evidence_count: int = 0,
) -> dict[str, Any]:
    """保存一次反思的范围和结果，避免重启后重复扫描全部经历。"""
    if status not in {"running", "completed", "failed"}:
        raise ValueError("status 必须是 running、completed 或 failed")
    ensure_personality_store()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO personality_reflection_runs (
                reflection_id, persona_instance_id, last_analyzed_sequence,
                this_run_start_sequence, this_run_end_sequence, status,
                started_at, completed_at, failure_reason, algorithm_version,
                accepted_evidence_count, rejected_evidence_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(reflection_id) DO UPDATE SET
                last_analyzed_sequence=excluded.last_analyzed_sequence,
                this_run_start_sequence=excluded.this_run_start_sequence,
                this_run_end_sequence=excluded.this_run_end_sequence,
                status=excluded.status, completed_at=excluded.completed_at,
                failure_reason=excluded.failure_reason,
                accepted_evidence_count=excluded.accepted_evidence_count,
                rejected_evidence_count=excluded.rejected_evidence_count
            """,
            (
                reflection_id, persona_instance_id, last_analyzed_sequence,
                this_run_start_sequence, this_run_end_sequence, status,
                started_at or _now(), completed_at, failure_reason,
                algorithm_version, accepted_evidence_count, rejected_evidence_count,
            ),
        )
    return get_reflection_run(reflection_id) or {}


def get_reflection_run(reflection_id: str) -> dict[str, Any] | None:
    ensure_personality_store()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM personality_reflection_runs WHERE reflection_id = ?",
            (str(reflection_id or "").strip(),),
        ).fetchone()
    return dict(row) if row else None


def get_last_reflection_run(persona_instance_id: str) -> dict[str, Any] | None:
    ensure_personality_store()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM personality_reflection_runs WHERE persona_instance_id = ? "
            "AND status = 'completed' ORDER BY completed_at DESC LIMIT 1",
            (str(persona_instance_id or "").strip(),),
        ).fetchone()
    return dict(row) if row else None
