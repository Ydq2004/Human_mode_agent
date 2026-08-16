import json
import re


def _parse_json_text(text: str) -> dict:
    """从 LLM 输出中解析 JSON；兼容 ```json 包裹的情况。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _fallback_signature(concept_name: str) -> dict:
    """提取失败时的保底签名，避免上层流程因为空结果崩掉。"""
    return {
        "subject": concept_name,
        "relation": "unknown",
        "object": None,
        "qualifier": None,
    }


def _clean_signature(value: dict, concept_name: str) -> dict:
    """清洗 signature，保证四个固定字段都存在。"""
    if not isinstance(value, dict):
        return _fallback_signature(concept_name)

    subject = str(value.get("subject") or concept_name).strip()
    relation = str(value.get("relation") or "unknown").strip()

    obj = value.get("object")
    if obj is not None:
        obj = str(obj).strip() or None

    qualifier = value.get("qualifier")
    if qualifier is not None:
        qualifier = str(qualifier).strip() or None

    return {
        "subject": subject,
        "relation": relation,
        "object": obj,
        "qualifier": qualifier,
    }


def extract_identity_signature(
    concept_name: str,
    summary: str,
    memory_type: str,
    llm,
) -> dict:
    """
    从候选认知中提取 identity_signature。

    注意：
    - 这里只做结构化提取，不判断是否重复。
    - 是否指向已有认知，由 identity_resolver 负责。
    - 这是框架层能力，不应包含具体 persona 的性格逻辑。
    """
    prompt = f"""
你是通用人格 Agent 框架中的“认知身份签名提取器”。

你的任务：
把一个候选认知转成稳定的 identity_signature。

你不是裁决器：
- 不判断这条认知是否应该写入。
- 不判断它是否已经存在。
- 不判断 same / related / new。
- 不合并认知。
- 不拆分多条认知。
- 不改写事实。
- 不根据常识补充输入中没有的信息。

【输入】
concept_name: {concept_name}
memory_type: {memory_type}
summary: {summary}

【输出要求】
只输出 JSON object，不要 markdown，不要解释。
必须只包含以下四个字段：

{{
  "subject": "...",
  "relation": "...",
  "object": "... 或 null",
  "qualifier": "... 或 null"
}}

【输入契约】

每次输入原则上应该只包含一个“原子认知”。

identity_signature 只能表达一个 subject-relation-object。
如果输入中包含多个并列事实、对立事实、多个核心 object 或多个独立关系：
- 不要把多个事实压进一个 signature。
- 不要把另一个事实塞进 qualifier。
- 在无法拆分的前提下，只提取最主要、最明确的一个认知。
- 如果无法判断哪个是主要认知，优先根据 concept_name 提取。
- 多事实拆分应由上游候选生成模块负责，不由你负责。

【核心目标】

identity_signature 不是摘要。
它用于查重、定位、检索和更新。

因此：
- subject / relation / object 表达认知的核心身份。
- qualifier 只表达区分同类认知所必需的限定条件。
- 不要把完整句子、评价、解释、情绪反应塞进字段里。

【提取流程】

第一步：确定认知类型

先根据 memory_type、concept_name 和 summary 判断它更像哪类认知：

1. 偏好/回避：
   某个主体喜欢、偏好、讨厌、回避、接受或拒绝某个对象。

2. 实体/身份：
   某个实体是什么、叫什么、属于谁、与谁同指、有什么别名。

3. 关系/归属：
   两个实体之间存在拥有、属于、称呼、亲属、同伴、关联等关系。

4. 互动模式：
   某个动作、短语、暗号、习惯表达某种请求、需求或互动意图。

5. 状态触发：
   某个条件、事件、行为会影响主体的状态、情绪或反应。

6. 提及/未知：
   输入只是提到某个对象，但没有说明它是什么、主体如何看待它、或它与其他对象的关系。

第二步：确定 subject

subject 是这条认知绑定的稳定主体。

选择规则：
- 如果认知描述的是某人的偏好、习惯、状态、请求、身份信息，subject 应是这个人。
- 如果认知描述的是一个独立实体本身，subject 应是这个实体。
- 如果认知描述的是别名、本名、外号、旧称呼、代称，subject 应是“被命名/被称呼”的稳定实体，不是别名本身。
- 如果是“某人提到 X”，subject 应是提到者，object 应是 X。
- 不要输出“我、你、他、她、它、这个、那个、自己”等代词作为 subject 或 object。
- 如果输入里有稳定称呼，应使用稳定称呼替换代词。
- 如果无法从输入恢复稳定主体，subject 使用 concept_name，不要猜测。

第三步：归一 relation

relation 必须是简短、稳定、可重复匹配的关系词。

优先使用以下关系词：

- 喜欢：明确喜欢、偏爱、爱吃、爱用、倾向选择。
- 讨厌：明确讨厌、不喜欢、反感、厌恶。
- 避免：不吃、不用、不碰、回避、拒绝、害怕某对象并表现为回避，但没有明确厌恶。
- 属于：拥有、归属、属于某人、是某人的。
- 是：身份、类别、本名、外号、别名、定义。
- 表达：动作、短语、暗号表达某种需求或意图。
- 请求：希望对方做某事、请求某种回应。
- 习惯：经常、总是、通常、倾向于在某情境下做某事。
- 影响：某条件、事件或行为触发/影响某种状态、情绪或反应。
- 关系：两者之间的关系、称呼关系、亲密关系。
- 提到：只是提及某对象，无法归入以上关系。
- unknown：确实无法判断。

关系归一原则：
- 情绪词不要直接等于偏好词。
- “害怕”如果表达对对象的回避倾向，优先用“避免”。
- “害怕/紧张/烦躁/安心”等如果表达某情境触发的状态，优先用“影响”。
- “想要”如果表达互动意图，优先用“请求”或“表达”，不要直接归为“喜欢”。
- “不吃/不用/不碰”如果没有明确厌恶，优先用“避免”，不要直接归为“讨厌”。

第四步：确定 object

object 是 relation 指向的核心对象。

选择规则：
- 偏好/回避类：object 是被喜欢、讨厌、避免、接受或拒绝的对象。
- 实体/身份类：object 是类别、身份、名字、别名、称呼或定义。
- 关系/归属类：object 是关系指向的另一方。
- 互动模式类：object 是被表达、请求或暗示的需求/意图。
- 状态触发类：object 是被影响或触发的状态、情绪、反应。
- 提及/未知类：object 是被提到的对象；如果没有提到者，subject 可以是该对象，object 填 null。
- 如果没有明确对象，填 null。

第五步：确定 qualifier

qualifier 只保留“区分同一 subject-relation-object 下不同认知”所必需的限定条件。

可以进入 qualifier：
- 时间、场景、条件、动作、口味、程度、身份限定、对象范围。

不要进入 qualifier：
- 另一个独立事实。
- 完整长句。
- 主观评价。
- 情绪渲染。
- 因果解释长段落。
- 输入中没有明确表达的常识补充。

如果 qualifier 对区分身份没有帮助，填 null。

【按 memory_type 校准】

memory_type=preference:
subject = 偏好主体
relation = 喜欢 / 讨厌 / 避免
object = 偏好或回避对象
qualifier = 口味、场景、程度、条件等

memory_type=entity:
subject = 被描述实体，或被命名的稳定主体
relation = 是 / 属于
object = 类别、身份、名字、别名、拥有者
qualifier = 本名、外号、旧称呼、归属条件等

memory_type=relationship:
subject = 关系的一方
relation = 关系 / 属于 / 是
object = 关系的另一方
qualifier = 关系类型或称呼限定

memory_type=interaction_pattern:
subject = 发出动作、短语或请求的人
relation = 表达 / 请求 / 习惯 / 影响
object = 需求、意图、互动目标或被触发状态
qualifier = 动作、暗号、触发场景

memory_type=event:
subject = 事件主体
relation = 提到 / 影响 / 请求 / 是
object = 事件对象
qualifier = 时间、地点、场景

memory_type=knowledge:
subject = 知识主题
relation = 是 / 属于 / 影响 / 提到
object = 被定义、归类或影响的对象
qualifier = 范围、条件、上下文

【抽象校准例子】

例子 1：偏好
输入：
concept_name: 用户偏爱清淡口味的早餐
memory_type: preference
summary: 用户偏爱清淡口味的早餐。
输出：
{{
  "subject": "用户",
  "relation": "喜欢",
  "object": "早餐",
  "qualifier": "清淡口味"
}}

例子 2：回避但不等于讨厌
输入：
concept_name: 用户不喝含糖饮料
memory_type: preference
summary: 用户说自己不喝含糖饮料。
输出：
{{
  "subject": "用户",
  "relation": "避免",
  "object": "含糖饮料",
  "qualifier": null
}}

例子 3：别名/称呼
输入：
concept_name: 用户的旧称呼
memory_type: entity
summary: A 是用户以前使用过的旧称呼。
输出：
{{
  "subject": "用户",
  "relation": "是",
  "object": "A",
  "qualifier": "旧称呼"
}}

例子 4：互动意图
输入：
concept_name: 某动作表示需要暂停
memory_type: interaction_pattern
summary: 用户说以后做出某动作就是想暂停一下。
输出：
{{
  "subject": "用户",
  "relation": "表达",
  "object": "暂停需求",
  "qualifier": "某动作"
}}

例子 5：状态触发
输入：
concept_name: 某条件下容易紧张
memory_type: interaction_pattern
summary: 用户在某条件下容易变得紧张。
输出：
{{
  "subject": "用户",
  "relation": "影响",
  "object": "紧张",
  "qualifier": "某条件下"
}}

例子 6：复合事实只能提取一个原子认知
输入：
concept_name: 用户喜欢 A 但不喜欢 B
memory_type: preference
summary: 用户喜欢 A，但不喜欢 B。
输出：
{{
  "subject": "用户",
  "relation": "喜欢",
  "object": "A",
  "qualifier": null
}}

请提取当前输入的 identity_signature。
"""

    try:
        json_llm = llm.bind(response_format={"type": "json_object"})
        response = json_llm.invoke(prompt)
        result = _parse_json_text(response.content)
        return _clean_signature(result, concept_name)
    except Exception as e:
        print(f"⚠️ identity_signature 提取失败，使用保底签名：{e}")
        return _fallback_signature(concept_name)