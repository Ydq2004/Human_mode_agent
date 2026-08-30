"""周期情景摘要的轻量调度入口。

本模块只在被启动流程或外部定时器调用时运行一次，不创建常驻线程，也不调用
LLM。它检查已经结束的日期/自然周/自然月，并根据来源切片是否变化决定跳过
还是生成新摘要。原始 ExperienceSlice 和旧摘要都不会被删除。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from memory.experience_store import list_experience_slices
from memory.experience_summary_builder import (
    build_daily_summary,
    build_monthly_summary,
    build_weekly_summary,
)
from memory.experience_summary_store import (
    ExperienceSummary,
    list_summaries,
    save_summary,
)


@dataclass(frozen=True)
class SummaryPeriod:
    """一个待检查的完整自然周期。"""

    summary_type: str
    start_day: date
    end_day: date


def _parse_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now()
    if not isinstance(now, datetime):
        raise TypeError("now 必须是 datetime 或 None")
    return now


def periods_to_check(now: datetime | None = None) -> tuple[SummaryPeriod, ...]:
    """返回昨天、上个完整自然周和上个完整自然月。"""
    current = _parse_now(now).date()
    yesterday = current - timedelta(days=1)

    current_week_start = current - timedelta(days=current.weekday())
    previous_week_end = current_week_start - timedelta(days=1)
    previous_week_start = previous_week_end - timedelta(days=6)

    current_month_start = current.replace(day=1)
    previous_month_end = current_month_start - timedelta(days=1)
    previous_month_start = previous_month_end.replace(day=1)

    return (
        SummaryPeriod("daily", yesterday, yesterday),
        SummaryPeriod("weekly", previous_week_start, previous_week_end),
        SummaryPeriod("monthly", previous_month_start, previous_month_end),
    )


def _period_bounds(period: SummaryPeriod) -> tuple[str, str]:
    return (
        f"{period.start_day.isoformat()}T00:00:00",
        f"{period.end_day.isoformat()}T23:59:59.999999",
    )


def _source_ids(rows) -> tuple[str, ...]:
    return tuple(row.experience.slice_id for row in rows)


def _has_current_summary(
    period: SummaryPeriod,
    source_ids: tuple[str, ...],
) -> bool:
    """已有摘要只有在来源切片完全一致时才算当前版本。"""
    start_at, end_at = _period_bounds(period)
    for summary in list_summaries(
        summary_type=period.summary_type,
        start_at=start_at,
        end_at=end_at,
        limit=100,
    ):
        if summary.source_slice_ids == source_ids:
            return True
    return False


def _build_period_summary(
    period: SummaryPeriod,
    rows,
    *,
    generated_at: str | None,
) -> ExperienceSummary:
    builders = {
        "daily": build_daily_summary,
        "weekly": build_weekly_summary,
        "monthly": build_monthly_summary,
    }
    return builders[period.summary_type](rows, generated_at=generated_at)


def generate_missing_period_summaries(
    *,
    now: datetime | None = None,
    thread_id: str | None = None,
    generated_at: str | None = None,
    limit: int = 1000,
) -> list[ExperienceSummary]:
    """补齐昨天、上周、上月中尚未覆盖当前来源的摘要。

    返回本次新生成（或发现已存在后复用）的摘要列表；没有经历的周期不返回。
    同一个进程或不同进程重复调用都可以安全执行。
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise ValueError("limit 必须是 1 到 10000 的整数")

    results: list[ExperienceSummary] = []
    for period in periods_to_check(now):
        start_at, end_at = _period_bounds(period)
        rows = list_experience_slices(
            start_at=start_at,
            end_at=end_at,
            thread_id=thread_id,
            limit=limit,
        )
        if not rows:
            continue

        source_ids = _source_ids(rows)
        if _has_current_summary(period, source_ids):
            continue

        summary = _build_period_summary(
            period,
            rows,
            generated_at=generated_at,
        )
        save_summary(summary)
        results.append(summary)

    return results

