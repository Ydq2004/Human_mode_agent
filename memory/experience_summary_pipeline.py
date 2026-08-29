"""情景摘要的读取、生成和保存流程。

本模块是调度层的最小闭环：它不启动线程、不调用 LLM，
只供日任务或后台调度器显式调用。原始 ExperienceSlice 永远不被修改。
"""

from __future__ import annotations

from datetime import datetime

from memory.experience_store import list_experience_slices
from memory.experience_summary_builder import build_daily_summary
from memory.experience_summary_store import (
    ExperienceSummary,
    get_summary,
    save_summary,
)


def generate_daily_summary(
    day: str,
    *,
    thread_id: str | None = None,
    generated_at: str | None = None,
    limit: int = 1000,
) -> ExperienceSummary | None:
    """生成并保存某一天的摘要；当天没有经历时返回 ``None``。

    ``day`` 使用 ``YYYY-MM-DD``。同一天如果后来新增了切片，来源 id
    会发生变化，生成器会产生一个新的摘要 id；旧摘要仍保留，便于追溯。
    """
    try:
        parsed_day = datetime.strptime(str(day).strip(), "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError("day 必须是 YYYY-MM-DD 格式") from exc

    day_text = parsed_day.strftime("%Y-%m-%d")
    rows = list_experience_slices(
        start_at=f"{day_text}T00:00:00",
        end_at=f"{day_text}T23:59:59.999999",
        thread_id=thread_id,
        limit=limit,
    )
    if not rows:
        return None

    summary = build_daily_summary(
        rows,
        generated_at=generated_at,
    )
    save_summary(summary)
    # 重复调度时返回数据库中的权威版本，避免 generated_at 等元数据
    # 让调用方误以为同一份摘要内容发生了变化。
    return get_summary(summary.summary_id) or summary
