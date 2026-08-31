"""用独立 LLM 生成周/月深层摘要。

LLM 只负责把原始 ExperienceSlice 归纳成可读的周期观察；来源切片、时间范围
和摘要 id 始终由代码决定，摘要也不会反写认知、情绪或人格状态。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Sequence

from memory.experience_store import StoredExperienceSlice
from memory.experience_summary_builder import (
    _as_slice_list,
    _default_summary_id,
    _event_range,
    _render_slice,
)
from memory.experience_summary_store import ExperienceSummary


SUMMARY_LLM_ALGORITHM_VERSION = "experience-summary-llm-v1"
SUMMARY_LLM_PROMPT_VERSION = "experience-summary-prompt-v1"


def _response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        ).strip()
    return str(content or "").strip()


def _parse_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.S)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("摘要 LLM 返回的顶层结构不是对象")
    return value


def build_deep_period_summary(
    slices: Sequence[StoredExperienceSlice],
    *,
    summary_type: str,
    llm: Any,
    summary_id: str | None = None,
    generated_at: str | None = None,
) -> ExperienceSummary:
    """从原始切片生成 weekly/monthly LLM 摘要。"""
    if summary_type not in {"weekly", "monthly"}:
        raise ValueError("LLM 深层摘要只支持 weekly 或 monthly")
    items = _as_slice_list(slices)
    start_at, end_at = _event_range(items)
    source_ids = tuple(item.experience.slice_id for item in items)
    rendered = []
    for number, item in enumerate(items, start=1):
        line, _ = _render_slice(number, item, text_limit=500)
        rendered.append(line)

    prompt = f"""你是项目的周期经历整理器。请只根据下面的原始 ExperienceSlice，生成 {summary_type} 周期摘要。
必须返回 JSON 对象：{{"summary_text":"...", "uncertainties":["..."]}}
要求：
1. summary_text 只总结确实发生的事件、Agent 实际行动、工具返回和可从多条经历直接观察到的主题/任务进展。
2. 可以指出重复出现的行为模式，但必须用“记录显示/多次出现”这类有来源的说法。
3. 不得断言关系升级、人格改变、内心情绪或用户未明确说过的事实；这些内容放入 uncertainties。
4. 不要编造不存在的事件、时间、工具结果或因果关系。
5. uncertainties 必须是字符串数组；无法确定的内容明确写“未确认”。
6. 不要输出 Markdown、解释或 JSON 之外的文字。

原始经历：
""" + "\n".join(rendered)
    response = llm.invoke(prompt)
    data = _parse_json(_response_text(response))
    text = re.sub(r"\s+", " ", str(data.get("summary_text") or "")).strip()
    if not text:
        raise ValueError("摘要 LLM 未返回 summary_text")
    uncertainties = data.get("uncertainties")
    if isinstance(uncertainties, str):
        uncertainties = [uncertainties]
    if not isinstance(uncertainties, list):
        uncertainties = []
    uncertainties = tuple(str(item).strip() for item in uncertainties if str(item).strip())
    return ExperienceSummary(
        summary_id=summary_id or _default_summary_id(summary_type, start_at, end_at, source_ids),
        summary_type=summary_type,
        start_at=start_at,
        end_at=end_at,
        source_slice_ids=source_ids,
        summary_text=text[:4000],
        uncertainties=uncertainties,
        algorithm_version=SUMMARY_LLM_ALGORITHM_VERSION,
        prompt_version=SUMMARY_LLM_PROMPT_VERSION,
        generated_at=generated_at or datetime.now().isoformat(),
    )

