"""运行一次人格反思闭环（P2）。

这个模块是后台流程的编排器：它读取新增的原始 ExperienceSlice，调用候选
生成器，再把候选交给代码验证，最后追加保存合格的 trait_evidence。它不在
前台每轮调用，也不直接写入或修改 PersonalitySnapshot。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from memory import experience_store, personality_store
from memory.personality_calculator import calculate_personality_snapshot
from memory.personality_reflection_llm import generate_trait_candidates
from memory.personality_store import TraitEvidence
from memory.personality_validator import validate_reflection_batch


REFLECTION_ALGORITHM_VERSION = "personality-reflection-v1"
DEFAULT_MIN_NEW_SLICES = 30
DEFAULT_MIN_INTERVAL_DAYS = 7


def _parse_time(value: str | datetime | None) -> datetime | None:
    """把常见 ISO 时间统一成可比较的无时区时间。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is not None:
        result = result.astimezone(timezone.utc).replace(tzinfo=None)
    return result


def _now(now: str | datetime | None) -> datetime:
    return _parse_time(now) or datetime.now()


def _candidate_key(item: dict[str, Any], persona_instance_id: str) -> str:
    """根据证据内容生成稳定键，保证重跑不会插入第二条相同证据。"""
    material = {
        "persona_instance_id": persona_instance_id,
        "trait_id": item["trait_id"],
        "direction": item["direction"],
        "scope": item["scope"],
        "behavior_source": item["behavior_source"],
        "evidence_slice_ids": sorted(item["evidence_slice_ids"]),
        "counter_evidence_slice_ids": sorted(item["counter_evidence_slice_ids"]),
        "algorithm_version": REFLECTION_ALGORITHM_VERSION,
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _scope_slices(
    *,
    thread_id: str | None,
    last_sequence: int | None,
) -> list[experience_store.StoredExperienceSlice]:
    return experience_store.list_experience_slices(
        thread_id=thread_id,
        after_sequence=last_sequence,
        limit=1000,
    )


def run_personality_reflection(
    *,
    persona_instance_id: str,
    llm: Any,
    thread_id: str | None = None,
    min_new_slices: int = DEFAULT_MIN_NEW_SLICES,
    min_interval_days: float = DEFAULT_MIN_INTERVAL_DAYS,
    now: str | datetime | None = None,
    reprocess: bool = False,
) -> dict[str, Any]:
    """按触发条件运行一次反思，并返回可记录/可测试的结果字典。

    触发条件很保守：默认至少 30 条新经历，并且距上次完成反思至少 7 天；
    第一次反思则要求这批经历自身至少跨 7 天。失败不会推进已分析序号。
    ``reprocess=True`` 只用于人工补测/提示词修正，会显式忽略已有水位，
    不应作为日常调度默认值。
    """
    instance_id = str(persona_instance_id or "").strip()
    if not instance_id:
        raise ValueError("persona_instance_id 不能为空")
    scoped_thread_id = str(thread_id or "").strip()
    if not scoped_thread_id:
        # event_sequence 目前只在单个 thread 内递增；没有线程边界时，
        # 不允许把不同会话混在同一条人格反思水位中。
        raise ValueError("thread_id 不能为空；跨线程反思需要单独设计全局水位")
    if isinstance(min_new_slices, bool) or not isinstance(min_new_slices, int) or min_new_slices < 1:
        raise ValueError("min_new_slices 必须是正整数")
    if min_interval_days < 0:
        raise ValueError("min_interval_days 不能小于 0")

    personality_store.ensure_personality_store()
    if personality_store.get_personality_seed(instance_id) is None:
        return {"status": "skipped", "reason": "no_personality_seed", "accepted_count": 0}
    if personality_store.get_running_reflection_run(instance_id, thread_id=scoped_thread_id):
        return {"status": "skipped", "reason": "already_running", "accepted_count": 0}

    latest = personality_store.get_last_reflection_run(instance_id, thread_id=scoped_thread_id)
    last_sequence = None
    if not reprocess and latest and latest.get("last_analyzed_sequence") is not None:
        last_sequence = int(latest["last_analyzed_sequence"])
    slices = _scope_slices(thread_id=scoped_thread_id, last_sequence=last_sequence)
    if not slices:
        return {"status": "skipped", "reason": "no_new_slices", "accepted_count": 0}
    if len(slices) < min_new_slices:
        return {
            "status": "skipped",
            "reason": "insufficient_new_slices",
            "new_slice_count": len(slices),
            "accepted_count": 0,
        }

    event_times = [_parse_time(item.experience.perception_event.occurred_at) for item in slices]
    if any(item is None for item in event_times):
        return {"status": "skipped", "reason": "invalid_slice_time", "accepted_count": 0}
    current_time = _now(now)
    if latest and not reprocess:
        completed_at = _parse_time(latest.get("completed_at"))
        if completed_at is None or current_time - completed_at < timedelta(days=min_interval_days):
            return {"status": "skipped", "reason": "reflection_interval_not_reached", "accepted_count": 0}
    elif max(event_times) - min(event_times) < timedelta(days=min_interval_days):
        return {"status": "skipped", "reason": "initial_time_span_not_reached", "accepted_count": 0}

    sequences = [item.event_sequence for item in slices]
    if any(sequence is None for sequence in sequences):
        return {"status": "skipped", "reason": "missing_event_sequence", "accepted_count": 0}
    start_sequence = min(int(sequence) for sequence in sequences)
    end_sequence = max(int(sequence) for sequence in sequences)
    reflection_id = f"reflection_{uuid4().hex}"
    personality_store.record_reflection_run(
        reflection_id=reflection_id,
        persona_instance_id=instance_id,
        thread_id=scoped_thread_id,
        status="running",
        last_analyzed_sequence=last_sequence,
        this_run_start_sequence=start_sequence,
        this_run_end_sequence=end_sequence,
        started_at=current_time.isoformat(),
        algorithm_version=REFLECTION_ALGORITHM_VERSION,
    )

    try:
        snapshot = calculate_personality_snapshot(instance_id)
        raw_candidates = generate_trait_candidates(llm, slices, current_snapshot=snapshot)
        valid, rejected = validate_reflection_batch(raw_candidates, persona_instance_id=instance_id)
        written = 0
        for item in valid:
            dedupe_key = _candidate_key(item, instance_id)
            evidence = TraitEvidence(
                evidence_id=f"trait_evidence_{dedupe_key[:24]}",
                trait_id=item["trait_id"],
                direction=item["direction"],
                strength=item["strength"],
                scope=item["scope"],
                behavior_source=item["behavior_source"],
                evidence_slice_ids=item["evidence_slice_ids"],
                counter_evidence_slice_ids=item["counter_evidence_slice_ids"],
                confidence=item["confidence"],
                dedupe_key=dedupe_key,
                algorithm_version=REFLECTION_ALGORITHM_VERSION,
                uncertainty=item["uncertainty"],
                created_at=current_time.isoformat(),
                persona_instance_id=instance_id,
            )
            before = personality_store.list_trait_evidence(persona_instance_id=instance_id, limit=1000)
            personality_store.append_trait_evidence(evidence)
            if not any(row.dedupe_key == dedupe_key for row in before):
                written += 1

        # 快照是证据聚合后的只读版本记录；它不回写角色卡，也不替代 seed/evidence。
        snapshot_after = calculate_personality_snapshot(instance_id)
        personality_store.save_personality_snapshot(
            persona_instance_id=instance_id,
            trait_values=snapshot_after,
            as_of_timestamp=max(item.experience.perception_event.occurred_at for item in slices),
            algorithm_version="personality-snapshot-v1",
            created_at=current_time.isoformat(),
        )

        personality_store.record_reflection_run(
            reflection_id=reflection_id,
            persona_instance_id=instance_id,
            thread_id=scoped_thread_id,
            status="completed",
            last_analyzed_sequence=end_sequence,
            this_run_start_sequence=start_sequence,
            this_run_end_sequence=end_sequence,
            started_at=current_time.isoformat(),
            completed_at=current_time.isoformat(),
            algorithm_version=REFLECTION_ALGORITHM_VERSION,
            accepted_evidence_count=written,
            rejected_evidence_count=len(rejected),
        )
        return {
            "status": "completed",
            "reflection_id": reflection_id,
            "analyzed_slice_count": len(slices),
            "last_analyzed_sequence": end_sequence,
            "accepted_count": written,
            "rejected_count": len(rejected),
            "rejections": rejected,
        }
    except Exception as exc:
        personality_store.record_reflection_run(
            reflection_id=reflection_id,
            persona_instance_id=instance_id,
            thread_id=scoped_thread_id,
            status="failed",
            last_analyzed_sequence=last_sequence,
            this_run_start_sequence=start_sequence,
            this_run_end_sequence=end_sequence,
            started_at=current_time.isoformat(),
            completed_at=current_time.isoformat(),
            failure_reason=f"{type(exc).__name__}: {exc}",
            algorithm_version=REFLECTION_ALGORITHM_VERSION,
        )
        return {
            "status": "failed",
            "reflection_id": reflection_id,
            "reason": f"{type(exc).__name__}: {exc}",
            "accepted_count": 0,
            "last_analyzed_sequence": last_sequence,
        }
