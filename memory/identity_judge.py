import json
import re

from memory.schema import MemoryEntity


def _parse_json_text(text: str) -> dict:
    """解析 LLM 返回的 JSON，兼容 ```json 包裹。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _entity_brief(entity: MemoryEntity) -> dict:
    """只给 judge 看裁决所需信息，避免上下文太胖。"""
    return {
        "concept_id": entity.concept_id,
        "canonical_name": entity.canonical_name,
        "aliases": entity.aliases,
        "memory_type": entity.memory_type,
        "identity_signature": entity.identity_signature,
        "summary": entity.summary,
    }


def _clean_decision(value: str) -> str:
    """限制 judge 的最终决策集合。"""
    value = str(value or "").strip().lower()
    if value in {"same", "related", "new"}:
        return value
    return "new"


VALID_RELATION_TYPES = {
    "related_to",
    "belongs_to",
    "refers_to",
    "similar_to",
    "none",
}

VALID_RELATION_DIRECTIONS = {
    "candidate_to_existing",
    "existing_to_candidate",
    "symmetric",
    "none",
}


def _clean_relation_type(value: str, decision: str) -> str:
    """relation_type 只描述一跳关系，不让 LLM 扩展关系类型集合。"""
    value = str(value or "none").strip()

    if decision != "related":
        return "none"

    if value in VALID_RELATION_TYPES and value != "none":
        return value

    # related 但关系类型不合法时，保守落到最泛化的一跳关系。
    return "related_to"


def _clean_relation_direction(value: str, decision: str) -> str:
    """限制关系方向；same/new 没有关系，必须返回 none。"""
    if decision != "related":
        return "none"

    value = str(value or "").strip().lower()
    if value in VALID_RELATION_DIRECTIONS and value != "none":
        return value
    return "symmetric"


def _memory_types_allow_same(candidate: dict, entity: MemoryEntity) -> bool:
    """
    same 表示同一条长期认知，因此双方类型必须一致。

    具体事件和由事件体现出的稳定偏好、关系或互动模式可以 related，
    但不能因为主体和主题相近就合并成同一个实体。字段缺失时不在这里
    擅自裁决，继续保留 LLM 的语义判断。
    """
    candidate_type = str(candidate.get("memory_type") or "").strip()
    entity_type = str(entity.memory_type or "").strip()

    if not candidate_type or not entity_type:
        return True

    return candidate_type == entity_type

def judge_ambiguous_identity(
    candidate: dict,
    candidate_entities: list[MemoryEntity],
    llm,
) -> dict:
    """
    对 resolver 无法确定的候选进行语义裁决。

    注意：
    - 不负责召回候选。
    - 不负责写库。
    - 只把 ambiguous 判成 same / related / new。
    """
    if not candidate_entities:
     return {
         "decision": "new",
         "target_concept_id": None,
         "relation_type": "none",
         "relation_direction": "none",
         "reason": "没有候选实体可判定",
        }

    prompt = f"""
你是通用人格 Agent 框架中的“认知身份裁决器”。

你的任务：
判断【待写入候选认知】和【已有认知候选列表】之间的身份关系。

你只能输出 JSON object。

【重要边界】
- 你不负责判断这条记忆是否应该写入。
- 你不负责改写记忆。
- 你不负责召回候选。
- 你不负责写入数据库。
- 你只判断身份关系：same / related / new。
- 不要根据角色性格判断情绪。
- 不要使用通用常识额外扩写事实。
- 只根据输入中给出的认知内容判断。
- 不要输出 confidence，置信度由系统层负责。

【裁决流程】

第一步：判断 same

只有当待写入候选认知和某个已有认知表达的是同一个长期认知时，才输出 same。

允许措辞不同，但必须满足以下条件之一：
- 两者描述同一个 subject、relation、object，且限定条件不冲突。
- 候选认知是已有认知的重述、同义表达、补充细节或更具体表达，并且不应形成两个独立认知。
- 两者指向同一个稳定实体、同一个偏好、同一个关系、同一个事件或同一个互动模式。
- 两者的 memory_type 必须一致。一次具体 event 与由它体现出的 preference、
  relationship 或 interaction_pattern 不是同一认知；有直接联系时判 related。

如果只是 subject 相同、relation 相同、memory_type 相同，不能判定 same。

第二步：判断 related

如果不是 same，只有存在强语义关联时，才输出 related。

强语义关联包括：
- 实体-类别关系：候选的 object 是已有实体的类别，或已有实体属于候选的 object。
- 指代/别名关系：一方是另一方的别名、代称、旧称呼、同指对象。
- 组成/归属关系：一方属于另一方、由另一方拥有、包含在另一方中。
- 事件/互动关系：两条认知描述同一事件、同一互动模式、同一具体对象的不同侧面。
- 解释/补充关系：一条认知能直接解释另一条认知里的具体对象、行为或关系。

related 表示“有关但不是同一个认知”。它不是泛化相似，也不是因为句式相似就建立关系。

第三步：排除弱相似

以下情况必须输出 new，而不是 related：
- 只是同一个 subject。
- 只是同一个 relation。
- 只是同一个 memory_type。
- 只是句式结构相似。
- 只是同一个人拥有多个不同偏好。
- 只是同一个人讨厌多个不同对象。
- 只是都表达情绪、习惯、请求，但对象不同且没有明确语义连接。
- 只是两个对象都属于很宽泛的日常类别，但输入中没有直接关系。

第四步：选择 target_concept_id

如果 decision 是 same 或 related，只能从【已有认知候选列表】中选择一个最相关的 concept_id。
如果没有足够依据选择任何候选，必须输出 new。
不要发明 concept_id。

第五步：选择 relation_type

只有 decision=related 时，relation_type 才能不是 none。

relation_type 的选择规则：
- related_to：有明确语义关联，但其他类型不准确时使用。
- belongs_to：实体属于类别、成员属于集合、对象属于拥有者。
- refers_to：别名、代称、旧称呼、同指对象。
- similar_to：只用于两个具体对象确实高度相似，不能用于同一主体的不同偏好、句式相似或弱相似。
- none：decision 是 same 或 new 时使用。

第六步：选择 relation_direction

关系方向只描述候选与已有认知两个端点的语义方向：
- candidate_to_existing：候选认知指向已有认知。
- existing_to_candidate：已有认知指向候选认知。
- symmetric：两者对称相关。
- none：decision 是 same 或 new 时使用。

belongs_to / refers_to 必须明确选择前两种之一；如果证据不足，仍输出 related，
但 relation_direction 使用 symmetric，代码层会把关系类型降级为 related_to，禁止猜方向。
similar_to 必须使用 symmetric。

【校准例子】

same:
“主人喜欢酸甜橘子”
vs
“主人对橘子的偏好”
=> same

related:
“主人喜欢狗”
vs
“大黄是主人养的狗”
=> related

new:
“主人喜欢橘子”
vs
“主人喜欢科幻电影”
=> new

new:
“主人讨厌苦瓜”
vs
“主人讨厌下雨天”
=> new

same:
“以后我张开双臂就是想要拥抱”
vs
“主人通过张开双臂表达拥抱需求”
=> same

related:
“主人这次因助手错误回答而生气”（event）
vs
“主人不喜欢被欺骗”（preference）
=> related

【待写入候选认知】
{json.dumps(candidate, ensure_ascii=False, indent=2)}

【已有认知候选列表】
{json.dumps([_entity_brief(entity) for entity in candidate_entities], ensure_ascii=False, indent=2)}

【输出格式】
{{
  "decision": "same | related | new",
  "target_concept_id": "如果 decision 为 same 或 related，填最相关的已有认知 concept_id；否则 null",
  "relation_type": "related_to | belongs_to | refers_to | similar_to | none",
  "relation_direction": "candidate_to_existing | existing_to_candidate | symmetric | none",
  "reason": "一句话说明"
}}
"""

    try:
        json_llm = llm.bind(response_format={"type": "json_object"})
        response = json_llm.invoke(prompt)
        result = _parse_json_text(response.content)
    except Exception as e:
      print(f"⚠️ identity judge 失败，保守返回 new：{e}")
      return {
        "decision": "new",
        "target_concept_id": None,
        "relation_type": "none",
        "relation_direction": "none",
        "reason": "identity judge 调用失败",
      }

    decision = _clean_decision(result.get("decision"))
    target_concept_id = result.get("target_concept_id")

    valid_ids = {entity.concept_id for entity in candidate_entities}
    if decision in {"same", "related"} and target_concept_id not in valid_ids:
        decision = "new"
        target_concept_id = None

    if decision == "same" and target_concept_id:
        target_entity = next(
            entity
            for entity in candidate_entities
            if entity.concept_id == target_concept_id
        )
        if not _memory_types_allow_same(candidate, target_entity):
            # LLM 已确认两者语义接近，但跨类型不能是同一认知。
            # 保留这种直接联系，同时避免事件吞并稳定偏好或关系实体。
            decision = "related"

    relation_type = _clean_relation_type(result.get("relation_type"), decision)
    relation_direction = _clean_relation_direction(
        result.get("relation_direction"),
        decision,
    )

    return {
        "decision": decision,
        "target_concept_id": target_concept_id,
        "relation_type": relation_type,
        "relation_direction": relation_direction,
        "reason": str(result.get("reason", "")).strip(),
    }
