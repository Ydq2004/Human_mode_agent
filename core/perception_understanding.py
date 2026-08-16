"""
回应前的精简感知理解层。

输入是 PerceptionFrame，不假设事件来自用户文本。输出只保留当前情境、
能力约束、长期认知检索方向和会实质改变后续处理的重要不确定性。
"""

import json
import re
from time import perf_counter
from typing import Any

from core.perception import PerceptionFrame


_KNOWLEDGE_SCOPE_MODES = frozenset({
    "allowed",
    "source_limited",
    "mixed",
    "uncertain",
    "not_applicable",
})


def _parse_json_text(text: str) -> dict[str, Any]:
    """解析 JSON mode 输出，兼容模型偶尔附带的 Markdown 围栏。"""
    text = str(text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _response_diagnostics(
    response: Any,
    *,
    failure_stage: str,
    error: Exception | None = None,
) -> dict[str, Any]:
    """提取不包含 Prompt 和隐藏推理正文的安全诊断信息。

    不同 OpenAI 兼容服务会把结束原因、token 统计和拒绝信息放在不同
    字段中。这里仅读取排障需要的白名单字段；``reasoning_content`` 只记录
    是否存在及长度，不能把供应商隐藏推理直接打印出来。
    """
    content = getattr(response, "content", None)
    response_metadata = getattr(response, "response_metadata", None)
    additional_kwargs = getattr(response, "additional_kwargs", None)
    usage_metadata = getattr(response, "usage_metadata", None)

    if not isinstance(response_metadata, dict):
        response_metadata = {}
    if not isinstance(additional_kwargs, dict):
        additional_kwargs = {}
    if not isinstance(usage_metadata, dict):
        usage_metadata = {}

    text = content if isinstance(content, str) else ""
    finish_reason = response_metadata.get("finish_reason")
    if not finish_reason:
        token_usage = response_metadata.get("token_usage")
        if isinstance(token_usage, dict):
            finish_reason = token_usage.get("finish_reason")

    refusal = additional_kwargs.get("refusal")
    reasoning_content = additional_kwargs.get("reasoning_content")

    diagnostics = {
        "failure_stage": failure_stage,
        "error_type": type(error).__name__ if error else None,
        "error": str(error)[:300] if error else None,
        "content_type": type(content).__name__,
        "content_length": len(text),
        # repr 能让空格、换行等不可见字符显形；最多保留 300 字符。
        "content_preview": repr(text[:300]),
        "finish_reason": finish_reason,
        "model_name": response_metadata.get("model_name"),
        "token_usage": (
            response_metadata.get("token_usage")
            if isinstance(response_metadata.get("token_usage"), dict)
            else usage_metadata
        ),
        "refusal_present": bool(refusal),
        "refusal_preview": (
            str(refusal)[:300] if refusal else None
        ),
        "reasoning_content_present": bool(reasoning_content),
        "reasoning_content_length": (
            len(str(reasoning_content)) if reasoning_content else 0
        ),
    }
    return diagnostics


def _print_understanding_failure(diagnostics: dict[str, Any]) -> None:
    """用结构化 JSON 打印诊断，便于从长日志中单独搜索和比较。"""
    print("PerceptionUnderstanding 失败诊断：")
    print(json.dumps(
        diagnostics,
        # ASCII 转义避免供应商返回的特殊字符在 Windows GBK 终端中让
        # “诊断代码本身”再次抛 UnicodeEncodeError。
        ensure_ascii=True,
        indent=2,
        default=str,
    ))


def _print_empty_response_retry(diagnostics: dict[str, Any]) -> None:
    """记录空正文触发的重试；重试只读，不会重复执行工具或写入状态。"""
    print("PerceptionUnderstanding 空响应，准备重试：")
    print(json.dumps(
        diagnostics,
        ensure_ascii=True,
        indent=2,
        default=str,
    ))


def _print_retry_recovered(*, request_seconds: float) -> None:
    """只报告重试已恢复，不打印成功响应正文或隐藏推理。"""
    print("PerceptionUnderstanding 重试恢复：")
    print(json.dumps(
        {
            "retry_result": "success",
            "attempt": 2,
            "request_seconds": round(request_seconds, 3),
        },
        ensure_ascii=True,
        indent=2,
    ))


def _clean_text(value: Any, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def _clean_string_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []

    result = []
    for item in value:
        text = _clean_text(item)
        if text and text not in result:
            result.append(text)

    return result[:limit]


def _clean_filters(value: Any) -> dict[str, str]:
    """
    filters 只是检索注意力线索，不是身份裁决。

    只保留 retrieval_engine 当前真实支持的字段。
    """
    if not isinstance(value, dict):
        return {}

    filters = {}
    for key in ("subject", "object", "memory_type"):
        cleaned = _clean_text(value.get(key), limit=100)
        if cleaned:
            filters[key] = cleaned

    return filters


def _allowed_domains(frame: PerceptionFrame) -> list[str]:
    """读取角色卡声明的通用知识领域，不自行扩展领域含义。"""
    knowledge_boundary = frame.persona_context.get("knowledge_boundary")
    if not isinstance(knowledge_boundary, dict):
        return []

    return _clean_string_list(
        knowledge_boundary.get("allowed_domains"),
        limit=20,
    )


def _clean_knowledge_scope(
    value: Any,
    frame: PerceptionFrame,
) -> dict[str, Any]:
    """
    清洗本轮知识来源边界。

    allowed_domain_matches 只能引用角色卡中真实存在的领域。模型无法可靠
    分类时保留 uncertain，不能由清洗层猜测当前话题属于哪个领域。
    """
    if not isinstance(value, dict):
        return {
            "mode": "uncertain",
            "allowed_domain_matches": [],
            "restricted_topics": [],
            "reason": "感知理解器没有给出可靠的知识来源范围。",
        }

    raw_mode = _clean_text(value.get("mode"), limit=30)
    mode = (
        raw_mode
        if raw_mode in _KNOWLEDGE_SCOPE_MODES
        else "uncertain"
    )

    allowed_lookup = {
        domain.casefold(): domain
        for domain in _allowed_domains(frame)
    }
    allowed_matches = []
    for item in _clean_string_list(
        value.get("allowed_domain_matches"),
        limit=20,
    ):
        matched = allowed_lookup.get(item.casefold())
        if matched and matched not in allowed_matches:
            allowed_matches.append(matched)

    reason = _clean_text(value.get("reason"), limit=300)
    if mode == "uncertain" and raw_mode not in _KNOWLEDGE_SCOPE_MODES:
        reason = "感知理解器返回了无效的知识来源分类。"
    elif mode in {"allowed", "mixed"} and not allowed_matches:
        mode = "uncertain"
        reason = (
            "感知理解器声称需要允许领域知识，"
            "但没有给出角色卡白名单中的有效领域。"
        )

    return {
        "mode": mode,
        "allowed_domain_matches": allowed_matches,
        "restricted_topics": _clean_string_list(
            value.get("restricted_topics"),
            limit=5,
        ),
        "reason": reason or "模型未提供知识来源判断依据。",
    }


def _clean_activation_cues(
    value: Any,
    fallback_query: str,
) -> list[dict[str, Any]]:
    """清洗自动认知唤起线索，并尊重模型明确给出的空列表。"""
    cues = []
    seen = set()

    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue

            query = _clean_text(item.get("query"))
            if not query:
                continue

            filters = _clean_filters(item.get("filters"))
            dedupe_key = (
                query,
                filters.get("subject", ""),
                filters.get("object", ""),
                filters.get("memory_type", ""),
            )
            if dedupe_key in seen:
                continue

            seen.add(dedupe_key)
            cues.append({
                "query": query,
                "filters": filters,
                "derived_from": _clean_text(item.get("derived_from")),
            })

    # [] 是模型“不需要长期认知”的明确决定，清洗层不能反向造 query。
    if isinstance(value, list) and (cues or not value):
        return cues[:3]

    # 字段缺失、类型错误，或非空列表全部非法时，才保留最小召回入口。
    if fallback_query:
        return [{
            "query": fallback_query,
            "filters": {},
            "derived_from": "原始感知事件",
        }]

    return []


def _fallback_understanding(
    frame: PerceptionFrame,
    reason: str,
) -> dict[str, Any]:
    """调用失败时不假装理解，只保留原始事件作为弱化唤起入口。"""
    event = frame.perception_event

    return {
        # 运行状态由代码产生，不能让失败的 LLM 自己声称成功。
        "understanding_status": "failed",
        "situated_understanding": (
            "目前只能确认收到该感知事件，具体意义仍需结合后续信息判断。"
        ),
        "knowledge_scope": {
            "mode": "uncertain",
            "allowed_domain_matches": [],
            "restricted_topics": [],
            "reason": "感知理解器调用失败，不能可靠确定本轮可用知识来源。",
        },
        # 失败时不能猜测事件是否要求了某种副作用；能力真值仍由
        # capability_snapshot 单独提供给主 Agent。
        "capability_constraints": [],
        "memory_activation_cues": [{
            "query": event.content,
            "filters": {},
            "derived_from": "原始感知事件",
        }],
        "uncertainties": [reason],
    }


def _clean_understanding(
    raw: dict[str, Any],
    frame: PerceptionFrame,
) -> dict[str, Any]:
    """清洗契约，不替模型补充语义判断。"""
    event = frame.perception_event

    return {
        "understanding_status": "normal",
        "situated_understanding": (
            _clean_text(raw.get("situated_understanding"))
            or "当前事件的具体意义仍不确定。"
        ),
        "knowledge_scope": _clean_knowledge_scope(
            raw.get("knowledge_scope"),
            frame,
        ),
        "capability_constraints": _clean_string_list(
            raw.get("capability_constraints"),
            limit=4,
        ),
        "memory_activation_cues": _clean_activation_cues(
            raw.get("memory_activation_cues"),
            fallback_query=event.content,
        ),
        "uncertainties": _clean_string_list(
            raw.get("uncertainties"),
            limit=4,
        ),
    }


def _build_understanding_prompt(frame: PerceptionFrame) -> str:
    """构造精简、来源无关的感知理解协议。"""
    return f"""
你是通用类人 Agent 框架中的 PerceptionUnderstanding（感知理解）模块。
输入是一份 PerceptionFrame，来源可能是用户文本、工具结果、视觉或听觉描述、
环境观察、动作反馈、系统状态变化、后台提醒或其他 Agent 消息。

你只负责：
1. 用一句简洁的话说明当前需要处理的情境。
2. 判断完成本轮回应时可以使用哪些知识来源。
3. 对照 capability_snapshot，指出当前请求涉及但 Agent 实际不具备的能力。
4. 判断当前事件可能让哪些个人长期认知自然浮现，并给出最多 3 条独立线索。
5. 保留会实质改变回应、认知唤起或行动的重要未知。

你不回复用户，不规划回复，不调用工具，不写记忆，不判断认知身份，
不决定 mood/energy，也不重新复述 perception_event 的表面内容。

【证据边界】
- perception_event 是本轮原始感知事实。
- working_context 是有限的近期上下文，不是本轮新事实。
- state_snapshot 只描述其 owner 的系统状态。
- capability_snapshot 是当前真实能力；未列出的能力不能假设存在。
- persona_context 只能影响注意方向和解释，不能制造事件事实。
- 常识、训练知识、可能动机、可能情绪和可能关系变化不能升级成事实。

【判断步骤】

第一步：判断当前情境。
- situated_understanding 只说明现在需要处理什么。
- 允许保守解释，但不能编造用户动机、稳定习惯、心理状态或关系变化。
- 意图模糊时明确写“意图未确认”，不要替主 Agent决定如何回复。

第二步：判断本轮知识来源范围。

这里判断的不是“话题名称是否在领域内”，而是“完成当前事件所需处理时，是否需要
调用角色允许领域之外的模型训练知识（参数知识）”。当前感知事件、带来源的
working_context、自然浮现的个人长期认知、真实工具结果和真实观察结果中明确
提供的内容，都不是参数知识。

角色卡中的 allowed_domains 只规定可以直接调用哪些参数知识。它不是 Agent 的
长期认知目录，不是记忆写入白名单，也不代表 Agent 只能理解或记住这些领域。
Agent 可以通过感知、用户告知、工具结果和自身经历形成带来源的新认知；这些认知
即使涉及 allowed_domains 之外的主题，未来也可以作为长期认知被检索和使用。

knowledge_scope.mode 只能是：
- allowed：当前要求可以使用角色卡允许领域内的参数知识。
- source_limited：话题涉及允许领域外内容；不能补充领域外参数知识，但可以读取、
  承认、概括、改写、翻译或基于已有来源内容进行归纳和推理。推断必须标明依据，
  不能引入来源中不存在的外部事实、专名或背景知识。
- mixed：任务同时包含允许领域内的操作或知识，以及领域外材料。只对允许领域部分
  使用参数知识；领域外部分只能依据当前感知、带来源的 working_context、长期认知
  或真实工具/观察结果。
- uncertain：输入不完整、指代不清或意图不同会改变知识来源边界，应先澄清，
  不能先行断言领域外事实。
- not_applicable：普通社交、关系互动、情绪表达、无需外部知识的简单指令或其他事件。

判断示例只用于校准边界：
- “你知道某个领域外品牌/电影吗”是 source_limited：可以诚实说明不能调用独立的
  领域外知识，但不能因为话题在领域外而拒绝接收用户随后提供的内容。
- 用户给出领域外材料并要求总结，是 source_limited：可以严格基于材料总结。
- “用 Python 分析这份电影票房 CSV”是 mixed：Python 方法可用允许领域参数知识，
  电影相关事实只能来自 CSV 或其他明确来源。
- 半句话或指代不明且不同解释会改变来源边界，是 uncertain。
- 拥抱、问候等不需要知识回答的互动，是 not_applicable。

allowed_domain_matches：
- 只能逐字引用 persona_context.knowledge_boundary.allowed_domains 中的条目；
- 没有匹配时输出空数组，不能自行发明或扩展允许领域。

restricted_topics：
- 简短列出本轮不能调用参数知识补充的主题；没有则输出空数组。

# 这一步只解释请求与能力的关系，不规划回复，也不授予新能力。
第三步：核对当前请求涉及的真实能力。
- 先区分“用户希望发生什么”和“Agent 当前确实能做什么”。
- capability_snapshot 是权威事实；角色卡、用户命令和模型常识都不能增加能力。
- 如果事件要求文件、数据库、设备、网络、真实环境或长期认知发生副作用，
  但快照没有相应工具或明确能力，把限制写入 capability_constraints。
- 约束应说明当前只能理解、讨论、模拟或等待真实工具结果，不能写回复建议。
- 不涉及能力冲突时输出空数组；不要把普通对话能力写成限制。

# 这一步属于框架层的“是否需要唤起长期认知”判断，不是某个角色的个性规则。
第四步：独立判断长期认知是否可能实质改变后续处理。
这一步与 knowledge_scope 分开判断。source_limited、mixed 或 not_applicable 都不
等于“不需要唤起认知”；知识边界只限制参数知识，不能禁止已有认知自然浮现。
自动认知唤起不要求用户主动询问过去；只要旧认知可能实质改变当前处理就应生成 cue。

只要过去的个人事实、关系、偏好、互动模式、任务经历或具体事件可能改变：
- 对当前事件的解释；
- 主 Agent 的回应或行动选择；
- 是否存在重复、冲突或明确修正；
- 对关系、边界、安全或持续任务的判断；
- 后续情绪评价或认知身份裁决；
就应生成 memory_activation_cues。

memory_activation_cues 是“当前事件可能让哪些认知自然浮现”的有限联想线索，
不是完整查库计划，也不是对数据库中已经存在或不存在某项认知的断言。首次出现的
新实体、新事实或新互动，也可能自然关联到同一对象、
相反事实、相关关系或既有边界，以便后续判断 create/update/reinforce。

以下场景通常应生成 cue：
- 当前事件明确提供了一个以后可能再次引用的实体、事实、偏好、承诺或任务信息；
- 当前事件包含直接针对 Agent 或关系对象的互动行为，而既有关系、边界、许可、
  过去反应或互动模式可能改变对它的理解；
- 当前事件明确修正、否定或重复确认了过去可能已经存在的内容；
- 当前事件虽然属于领域外主题，但用户或其他可靠来源已经提供了可形成认知的内容。
- 当前事件明确要求回忆、确认“是否还记得”或询问过去形成的个人认知。

working_context 只能帮助理解当前指代和事件连续性，不能代替自动认知唤起。
只要事件涉及稳定个人事实、偏好变化、关系/边界、旧认知修正或明确回忆请求，
即使近期上下文已经直接写出了答案，也必须生成对应 cue。是否能从近期上下文答出
和当前是否会自然联想到长期认知，是两个不同问题。

以下情况输出空数组：
- 普通寒暄且旧认知不会改变回应；
- 仅需通用训练知识的问题；
- working_context 已经提供处理本轮所需的完整临时事实，且事件不涉及稳定个人事实、
  偏好变化、关系/边界、旧认知修正或明确回忆请求，并且更早认知不可能改变处理；
- 只有词语联想，没有个人长期认知需求。

稳定个人事实第一次出现时，如果数据库中可能已有重复、相反或更具体的认知，
应尝试唤起相关事实；新实体或有复用价值的来源材料可能唤起同一对象；模糊动作或直接互动动作如果已有
互动模式、关系状态、许可或边界可能改变解释，也应生成线索。
不要仅因出现“我、主人、喜欢”等词就机械生成线索。

每条 cue：
- query 写清楚希望寻找的个人长期认知；
- filters 只有在当前事件有直接证据时才填写 subject/object/memory_type；
- derived_from 简短指出它来自当前事件或 working_context 的哪项依据。

第五步：保留重要不确定性。
- 只记录答案不同会改变回应、检索或行动的未知。
- 不要把“是否应该长期保存”列为主 Agent 需要追问的问题；记忆候选由回复后的
  后台框架自动评价，用户明确说“不记住/仅临时”时才把它作为事件事实保留。
- 不要为了显得谨慎罗列无后续作用的猜测。

【最终检查】
1. 是否把来源、Persona 或训练知识误当成事件事实？
2. 是否把“领域外话题”和“必须使用领域外参数知识”错误当成同一件事？
3. 是否错误地用 knowledge_scope 抑制了认知唤起，或漏掉了可能影响关系、边界、
   情绪评价和新认知身份裁决的旧认知？
4. 是否错误地因为 working_context 已有答案，就跳过了明确回忆请求或稳定认知唤起？
5. 是否把用户期望的副作用误写成 Agent 已具备或已经执行的能力？
6. 是否越权给出了回复建议、记忆写入决定或身份裁决？

【PerceptionFrame】
{json.dumps(frame.to_dict(), ensure_ascii=False, indent=2)}

只输出下面形状的 JSON object，不要输出 Markdown 或额外文字：
{{
  "situated_understanding": "当前需要处理的情境",
  "knowledge_scope": {{
    "mode": "allowed | source_limited | mixed | uncertain | not_applicable",
    "allowed_domain_matches": ["只能引用角色卡中的允许领域"],
    "restricted_topics": ["不能调用参数知识补充的主题"],
    "reason": "为什么本轮属于这个来源范围"
  }},
  "capability_constraints": [
    "当前请求涉及但 capability_snapshot 不支持的能力限制"
  ],
  "memory_activation_cues": [
    {{
      "query": "需要唤起的个人长期认知",
      "filters": {{}},
      "derived_from": "当前事件或工作上下文中的依据"
    }}
  ],
  "uncertainties": ["会实质改变后续处理的重要未知"]
}}
""".strip()


def understand_perception(
    frame: PerceptionFrame,
    llm,
    retry_llm=None,
) -> dict[str, Any]:
    """生成回应前的精简 PerceptionUnderstanding。"""
    prompt = _build_understanding_prompt(frame)

    request_started = perf_counter()
    try:
        json_llm = llm.bind(response_format={"type": "json_object"})
        
        response = json_llm.invoke(prompt)
    except Exception as exc:
        _print_understanding_failure({
            "failure_stage": "llm_call_failed",
            "attempt": 1,
            "request_seconds": round(
                perf_counter() - request_started,
                3,
            ),
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        })
        return _fallback_understanding(
            frame,
            reason="感知理解器调用失败，未对事件意图作额外推断。",
        )

    request_seconds = perf_counter() - request_started
    content = getattr(response, "content", None)
    if not isinstance(content, str) or not content.strip():
        diagnostics = _response_diagnostics(
            response,
            failure_stage="empty_response",
        )
        diagnostics.update({
            "attempt": 1,
            "request_seconds": round(request_seconds, 3),
            "retry_scheduled": True,
        })
        _print_empty_response_retry(diagnostics)

        # 感知理解调用只读取冻结的 PerceptionFrame，不执行工具、不写数据库，
        # 因此空正文时重试一次不会制造重复副作用。其他失败不在这里重试，
        # 避免用重复调用掩盖 JSON 契约或 schema 实现错误。
        retry_started = perf_counter()
        try:
            retry_client = retry_llm or llm
            json_retry_llm = retry_client.bind(
                response_format={"type": "json_object"}
            )
            response = json_retry_llm.invoke(prompt)
        except Exception as exc:
            _print_understanding_failure({
                "failure_stage": "llm_call_failed",
                "attempt": 2,
                "retry_trigger": "empty_response",
                "request_seconds": round(
                    perf_counter() - retry_started,
                    3,
                ),
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
            })
            return _fallback_understanding(
                frame,
                reason="感知理解器空响应重试失败，未对事件意图作额外推断。",
            )

        retry_seconds = perf_counter() - retry_started
        content = getattr(response, "content", None)
        if not isinstance(content, str) or not content.strip():
            diagnostics = _response_diagnostics(
                response,
                failure_stage="empty_response",
            )
            diagnostics.update({
                "attempt": 2,
                "request_seconds": round(retry_seconds, 3),
                "retry_scheduled": False,
                "retry_result": "empty_again",
            })
            _print_understanding_failure(diagnostics)
            return _fallback_understanding(
                frame,
                reason="感知理解器连续两次返回空内容，未对事件意图作额外推断。",
            )

        _print_retry_recovered(request_seconds=retry_seconds)

    try:
        raw = _parse_json_text(content)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        _print_understanding_failure(_response_diagnostics(
            response,
            failure_stage="json_parse_failed",
            error=exc,
        ))
        return _fallback_understanding(
            frame,
            reason="感知理解器返回内容不是有效 JSON。",
        )

    if not isinstance(raw, dict):
        _print_understanding_failure(_response_diagnostics(
            response,
            failure_stage="output_shape_failed",
        ))
        return _fallback_understanding(
            frame,
            reason="感知理解器输出不是 JSON object。",
        )

    try:
        return _clean_understanding(raw, frame)
    except Exception as exc:
        _print_understanding_failure(_response_diagnostics(
            response,
            failure_stage="schema_clean_failed",
            error=exc,
        ))
        return _fallback_understanding(
            frame,
            reason="感知理解器输出清洗失败，未采用未经校验的理解。",
        )
