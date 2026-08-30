"""用固定代码格式生成 ExperienceSlice 的情景摘要。

这是框架层的第一版摘要器：它不调用 LLM，也不做关系、人格或情绪裁决。
它只把不可变 ExperienceSlice 中已经发生的内容排成稳定的时间索引，
让后续的摘要检索可以快速定位原始经历；需要细节时仍然回查原始切片。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from memory.experience_store import StoredExperienceSlice
from memory.experience_summary_store import ExperienceSummary


SUMMARY_ALGORITHM_VERSION = "experience-summary-code-v1"


def _clean_text(value: Any, *, limit: int) -> str:
    """压成一行并限制长度，避免一条异常长消息撑爆工作上下文。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _as_slice_list(
    slices: Sequence[StoredExperienceSlice],
) -> list[StoredExperienceSlice]:
    """校验输入并按事件时间排序，避免调用方传入顺序影响摘要结果。"""
    items = list(slices or ())
    if not items:
        raise ValueError("生成摘要至少需要一条 ExperienceSlice")
    if not all(isinstance(item, StoredExperienceSlice) for item in items):
        raise TypeError("slices 中只能包含 StoredExperienceSlice")
    return sorted(
        items,
        key=lambda item: (
            item.experience.perception_event.occurred_at,
            item.event_sequence if item.event_sequence is not None else 2**63 - 1,
            item.experience.slice_id,
        ),
    )


def _event_range(items: Sequence[StoredExperienceSlice]) -> tuple[str, str]:
    times = [item.experience.perception_event.occurred_at for item in items]
    return min(times), max(times)


def _default_summary_id(
    summary_type: str,
    start_at: str,
    end_at: str,
    source_ids: Sequence[str],
) -> str:
    """根据覆盖范围和来源生成稳定 id；相同来源不会反复制造新摘要。"""
    material = "|".join((summary_type, start_at, end_at, *source_ids))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{summary_type}_{digest}"


def _string_items(value: Any) -> list[str]:
    """读取不确定性列表；非法形状不升级为确定事实。"""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _render_slice(
    number: int,
    item: StoredExperienceSlice,
    *,
    text_limit: int,
) -> tuple[str, list[str]]:
    """渲染单条切片，并返回这条切片明确声明的不确定性。"""
    experience = item.experience
    event = experience.perception_event
    actions = experience.response_or_actions
    observations = experience.observations

    action_text = "；".join(
        f"{_clean_text(action.action_type, limit=40)}："
        f"{_clean_text(action.content, limit=text_limit)}"
        for action in actions
    ) or "无记录"
    observation_text = "；".join(
        f"{_clean_text(observation.source, limit=30)}/"
        f"{_clean_text(observation.modality, limit=30)}："
        f"{_clean_text(observation.content, limit=text_limit)}"
        for observation in observations
    ) or "无记录"

    line = (
        f"[{number}] 时间 {event.occurred_at}；来源 {event.source}/"
        f"{event.modality}；原始感知：{_clean_text(event.content, limit=text_limit)}；"
        f"Agent行动：{action_text}；后续观察：{observation_text}"
    )

    uncertainties = [
        f"切片 {experience.slice_id}：{_clean_text(value, limit=160)}"
        for value in _string_items(
            experience.perception_understanding.get("uncertainties")
        )
    ]
    return line, uncertainties


def build_summary(
    slices: Sequence[StoredExperienceSlice],
    *,
    summary_type: str = "segment",
    summary_id: str | None = None,
    generated_at: str | None = None,
    text_limit: int = 240,
) -> ExperienceSummary:
    """从一组切片生成一份可追加保存的摘要。"""
    if summary_type not in {"segment", "daily", "weekly", "monthly"}:
        raise ValueError("生成器支持 segment、daily、weekly 和 monthly")
    if isinstance(text_limit, bool) or not isinstance(text_limit, int) or text_limit < 20:
        raise ValueError("text_limit 必须是大于等于 20 的整数")

    items = _as_slice_list(slices)
    start_at, end_at = _event_range(items)
    source_ids = tuple(item.experience.slice_id for item in items)
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("同一条 ExperienceSlice 不能在摘要中重复出现")

    lines = [
        "这是由代码根据原始 ExperienceSlice 整理的情景索引，不是新的事实判断。",
        f"覆盖时间：{start_at} 至 {end_at}；经历数量：{len(items)}。",
    ]
    uncertainties = [
        "本摘要只整理原始经历；不据此判断关系发展、人格变化或情绪结论。",
    ]
    for number, item in enumerate(items, start=1):
        line, item_uncertainties = _render_slice(
            number,
            item,
            text_limit=text_limit,
        )
        lines.append(line)
        uncertainties.extend(item_uncertainties)

    resolved_id = summary_id or _default_summary_id(
        summary_type,
        start_at,
        end_at,
        source_ids,
    )
    return ExperienceSummary(
        summary_id=resolved_id,
        summary_type=summary_type,
        start_at=start_at,
        end_at=end_at,
        source_slice_ids=source_ids,
        summary_text="\n".join(lines),
        uncertainties=tuple(dict.fromkeys(uncertainties)),
        algorithm_version=SUMMARY_ALGORITHM_VERSION,
        prompt_version=None,
        generated_at=generated_at or datetime.now().isoformat(),
    )


def build_segment_summary(
    slices: Sequence[StoredExperienceSlice],
    *,
    summary_id: str | None = None,
    generated_at: str | None = None,
    text_limit: int = 240,
) -> ExperienceSummary:
    """生成一段连续互动的摘要。"""
    return build_summary(
        slices,
        summary_type="segment",
        summary_id=summary_id,
        generated_at=generated_at,
        text_limit=text_limit,
    )


def build_daily_summary(
    slices: Sequence[StoredExperienceSlice],
    *,
    summary_id: str | None = None,
    generated_at: str | None = None,
    text_limit: int = 240,
) -> ExperienceSummary:
    """生成一天经历的摘要；时间范围由传入切片决定。"""
    items = _as_slice_list(slices)
    dates = {
        item.experience.perception_event.occurred_at.split("T", 1)[0]
        for item in items
    }
    if len(dates) != 1:
        raise ValueError("daily 摘要的切片必须属于同一个日期")
    return build_summary(
        items,
        summary_type="daily",
        summary_id=summary_id,
        generated_at=generated_at,
        text_limit=text_limit,
    )


def build_weekly_summary(
    slices: Sequence[StoredExperienceSlice],
    *,
    summary_id: str | None = None,
    generated_at: str | None = None,
    text_limit: int = 240,
) -> ExperienceSummary:
    """生成上一个完整自然周的摘要。"""
    return build_summary(
        slices,
        summary_type="weekly",
        summary_id=summary_id,
        generated_at=generated_at,
        text_limit=text_limit,
    )


def build_monthly_summary(
    slices: Sequence[StoredExperienceSlice],
    *,
    summary_id: str | None = None,
    generated_at: str | None = None,
    text_limit: int = 240,
) -> ExperienceSummary:
    """生成上一个完整自然月的摘要。"""
    return build_summary(
        slices,
        summary_type="monthly",
        summary_id=summary_id,
        generated_at=generated_at,
        text_limit=text_limit,
    )
