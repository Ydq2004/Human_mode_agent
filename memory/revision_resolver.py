"""同一认知的内容修订裁决。

IdentityJudge 回答“是不是同一条认知”；本模块只在身份已经确定为 same、
但新旧内容关系无法安全沿用旧评价时，回答“当前整合认知应怎样变化”。
它不查库、不写库，也不修改情绪印记。
"""

import json
import re
from typing import Any

from memory.schema import MemoryEntity


VALID_CONTENT_RELATIONS = {
    "duplicate",#重复,复制
    "extend",
    "contextualize",
    "replace",
    "correct",
    "conflict",
    "uncertain",
}
# 这些值描述“新证据与已有事实的内容关系”，不是身份判断结果。
# identity resolver 负责判断是不是同一个实体；本模块只决定同一实体的摘要如何处理。
_REVISING_RELATIONS = {
    "extend",
    "contextualize",
    "replace",#替换
    "correct",
}


def _parse_json_text(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise TypeError("revision resolver 必须返回 JSON object")
    return value


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _fallback(reason: str) -> dict[str, Any]:
    return {
        "content_relation": "uncertain",
        "new_information": "",
        "superseded_information": "",
        "revised_summary": "",
        "reason": reason,
    }


def resolve_memory_revision(
    *,
    current_entity: MemoryEntity,
    candidate: dict[str, Any],
    event_evidence: dict[str, Any],
    llm,
) -> dict[str, Any]:
    """裁决同一认知的新旧内容关系，失败时保守返回 uncertain。"""
    current_summary = str(current_entity.summary or "").strip()
    candidate_summary = str(candidate.get("summary") or "").strip()
    if candidate_summary and candidate_summary == current_summary:
        # 完全相同可以由普通代码确定，不必为重复内容调用一次 LLM。
        return {
            "content_relation": "duplicate",
            "new_information": "",
            "superseded_information": "",
            "revised_summary": "",
            "reason": "候选摘要与当前摘要完全相同",
        }

    if llm is None:
        return _fallback("没有可用的内容修订裁决模型")

    current_payload = {
        "concept_id": current_entity.concept_id,
        "canonical_name": current_entity.canonical_name,
        "memory_type": current_entity.memory_type,
        "identity_signature": current_entity.identity_signature,
        "summary": current_entity.summary,
        "revision": current_entity.revision,
    }
    prompt = f"""
你是通用类人 Agent 框架中的 MemoryRevisionResolver（认知内容修订裁决器）。

身份层已经确认候选与当前实体是同一条长期认知。你只判断新证据与当前整合
认知的内容关系，并在确实需要修订时给出新的完整整合摘要。

你不判断身份，不创建新实体，不修改情绪，不写数据库，不使用输入之外的知识。

content_relation 只能是：
- duplicate：没有新增事实，只是同义重复或再次确认。
- extend：增加可独立保留的新事实，旧事实仍成立。
- contextualize：增加时间、条件、场景或适用范围，旧事实需要更精确。
- replace：新状态明确取代旧状态，旧状态可作为历史背景保留。
- correct：来源明确否定旧说法并给出纠正。
- conflict：新旧陈述冲突，但证据不足以决定哪一个取代另一个。
- uncertain：输入不足，无法可靠判断。

规则：
1. 当前实体是提交时的最新版本，优先保护其中未被新证据明确撤回的信息。
2. Agent 自己的回复不能单独证明用户事实；以 perception_event 和 observations
   中的来源事实为准。
3. duplicate/conflict/uncertain 的 revised_summary 必须为空。
4. extend/contextualize/replace/correct 必须给出 revised_summary。它是当前实体与
   新证据整合后的完整摘要，不是只写新增半句话，也不能简单机械拼接。
5. revised_summary 必须保留来源边界；不要把“用户表示”改成无来源的客观真理。
6. replace/correct 只替代被证据明确覆盖的部分，其他信息继续保留。

【提交时最新实体】
{json.dumps(current_payload, ensure_ascii=False, indent=2)}

【本轮候选】
{json.dumps(candidate, ensure_ascii=False, indent=2)}

【原始事件证据】
{json.dumps(event_evidence, ensure_ascii=False, indent=2)}

只输出以下 JSON object：
{{
  "content_relation": "duplicate | extend | contextualize | replace | correct | conflict | uncertain",
  "new_information": "本轮确认的新增或替代信息；没有则空字符串",
  "superseded_information": "被明确取代或纠正的旧信息；没有则空字符串",
  "revised_summary": "需要修订时填写完整整合摘要，否则空字符串",
  "reason": "一句话说明直接证据"
}}
""".strip()

    try:
        response = llm.bind(
            response_format={"type": "json_object"}
        ).invoke(prompt)
        raw = _parse_json_text(response.content)
    except Exception as exc:
        return _fallback(
            f"内容修订裁决失败：{type(exc).__name__}: {exc}"
        )

    relation = _clean_text(raw.get("content_relation"), 30).lower()
    if relation not in VALID_CONTENT_RELATIONS:
        return _fallback("内容修订裁决返回了非法 relation")

    revised_summary = _clean_text(raw.get("revised_summary"), 900)
    # conflict/uncertain 没有足够证据覆盖当前认知，因此清空 revised_summary，
    # 让上层保留旧内容；只有四种真正修订关系才允许产生新完整摘要。
    if relation in _REVISING_RELATIONS and not revised_summary:
        return _fallback("需要修订内容，但模型没有给出完整整合摘要")
    if relation not in _REVISING_RELATIONS:
        revised_summary = ""

    return {
        "content_relation": relation,
        "new_information": _clean_text(raw.get("new_information"), 600),
        "superseded_information": _clean_text(
            raw.get("superseded_information"),
            600,
        ),
        "revised_summary": revised_summary,
        "reason": _clean_text(raw.get("reason"), 400),
    }
