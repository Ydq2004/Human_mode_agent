"""人格反思 LLM 的候选生成器（P2）。

LLM 只阅读一批原始 ExperienceSlice，并提出 trait_evidence 候选。
本模块不写数据库、不修改人格分数；候选必须再经过 personality_validator。
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from memory.experience_store import StoredExperienceSlice
from memory.personality_traits import list_all_traits


PROMPT_VERSION = "personality-reflection-prompt-v1"


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


def _parse_candidates(text: str) -> list[dict[str, Any]]:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", candidate, re.S)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("["), candidate.rfind("]")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    value = json.loads(candidate)
    if not isinstance(value, list):
        raise ValueError("人格反思 LLM 返回的顶层结构不是数组")
    return [item for item in value if isinstance(item, dict)]


def _render_slices(slices: Sequence[StoredExperienceSlice]) -> str:
    lines = []
    for item in slices:
        event = item.experience.perception_event
        actions = "；".join(action.content for action in item.experience.response_or_actions)
        lines.append(
            f"slice_id={item.experience.slice_id} sequence={item.event_sequence} "
            f"time={event.occurred_at} event={event.content} actions={actions or '无'}"
        )
    return "\n".join(lines)


def build_reflection_prompt(
    slices: Sequence[StoredExperienceSlice],
    *,
    current_snapshot: dict[str, float] | None = None,
) -> str:
    traits = "\n".join(
        f"- {item.trait_id}: {item.description}"
        for item in list_all_traits()
    )
    return f"""你是人格证据整理器，不是人格裁决器。
请只根据下面的原始 ExperienceSlice，提出可供代码验证的 trait_evidence 候选。

当前人格快照（仅作背景，不要修改）：{json.dumps(current_snapshot or {}, ensure_ascii=False)}

允许的特质：
{traits}

严格要求：
1. 只输出 JSON 数组；没有足够证据时输出 []。
2. 每个候选必须引用实际存在的 slice_id，不能编造 ID。
3. 至少引用 3 条相互独立的经历，不能依据单轮回复判断人格。
4. 用户明确要求后 Agent 直接执行，不算自主行为；不确定时写入 uncertainty。
5. 必须区分正面证据和反面证据；不能把同一 slice 同时放入两边。
6. 只能输出 trait_id、direction、strength、scope、behavior_source、
   evidence_slice_ids、counter_evidence_slice_ids、confidence、uncertainty。
7. 禁止输出最终人格分数、人格已经改变、关系结论或情绪结论。

特质定义：
{traits}

原始经历：
{_render_slices(slices)}
"""


def generate_trait_candidates(
    llm: Any,
    slices: Sequence[StoredExperienceSlice],
    *,
    current_snapshot: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """调用反思 LLM，仅返回结构化候选；不执行任何写入。"""
    if llm is None:
        raise ValueError("llm 不能为空")
    items = tuple(slices)
    if not items:
        return []
    response = llm.invoke(build_reflection_prompt(items, current_snapshot=current_snapshot))
    candidates = _parse_candidates(_response_text(response))
    return candidates
