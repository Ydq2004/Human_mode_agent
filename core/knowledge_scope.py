"""把本轮知识来源分类翻译成主 Agent 可执行的约束。"""


def format_knowledge_scope(understanding: dict | None) -> str:
    """返回动态提示词片段；没有可靠分类时不制造假的分类结果。"""
    if not isinstance(understanding, dict):
        return ""

    scope = understanding.get("knowledge_scope")
    if not isinstance(scope, dict):
        return ""

    mode = str(scope.get("mode") or "uncertain").strip()
    allowed_matches = scope.get("allowed_domain_matches")
    restricted_topics = scope.get("restricted_topics")
    reason = str(scope.get("reason") or "").strip()

    if not isinstance(allowed_matches, list):
        allowed_matches = []
    if not isinstance(restricted_topics, list):
        restricted_topics = []

    instructions = {
        "allowed": (
            "可以使用下列角色允许领域内的通用训练知识；其他领域仍不可补充。"
        ),
        "source_limited": (
            "不得调用领域外训练知识补充事实。可以正常接收、承认、概括、改写、"
            "翻译或分析当前感知事件、带来源的近期上下文、被唤起的长期认知和真实"
            "工具/观察结果中明确提供的内容。可以据此归纳和推理，但必须说明依据，"
            "不得引入来源中不存在的外部事实、专名或背景知识。不要仅因话题在领域"
            "外就拒绝处理这些已有材料。若事件发起方询问你是否独立知道某项领域外"
            "知识，应诚实说明知识边界，不得假装知道。"
        ),
        "mixed": (
            "把任务按来源分开：允许领域部分可以使用通用训练知识；领域外材料只能"
            "依据当前感知事件、带来源的近期上下文、被唤起的长期认知或真实工具/"
            "观察结果。不要用允许领域知识为领域外事实背书。"
        ),
        "uncertain": (
            "当前知识来源边界尚不确定。只回应已经确认的内容，必要时简短澄清；在"
            "澄清前不得断言领域外事实，也不要把不完整输入直接判成必须拒绝。"
        ),
        "not_applicable": (
            "本轮不需要调用外部知识。按当前互动自然回应，不要因为角色知识领域"
            "有限而拒绝普通交流。"
        ),
    }
    instruction = instructions.get(mode, instructions["uncertain"])

    allowed_text = "、".join(
        str(item) for item in allowed_matches if str(item).strip()
    ) or "无"
    restricted_text = "、".join(
        str(item) for item in restricted_topics if str(item).strip()
    ) or "无"

    return (
        "【本轮知识来源边界】\n"
        "这是框架根据当前感知给出的本轮强制来源约束。\n"
        "它只限制无来源的模型训练知识，不限制当前事件、近期上下文、长期认知或真实工具/观察结果，"
        "也不决定本轮内容是否值得检索或形成记忆。\n"
        f"范围模式：{mode}\n"
        f"允许领域匹配：{allowed_text}\n"
        f"受限主题：{restricted_text}\n"
        "分类依据（仅解释分类，不是新增事件事实）："
        f"{reason or '未提供'}\n"
        f"执行要求：{instruction}\n\n"
    )
