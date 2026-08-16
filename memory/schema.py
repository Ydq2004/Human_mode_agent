from dataclasses import dataclass, field


@dataclass
class MemoryEntity:
    concept_id: str
    canonical_name: str   
    aliases: list[str]            #别名
    memory_type: str              #记忆类型
    identity_signature: dict      #身份签名
    summary: str                  #总结
    tags: list[str] = field(default_factory=list)
    # 情绪印记允许保留小数；代码规则层负责最终范围钳制。
    emotion_score: float = 50.0
    emotion_label: str = "中性"
    mention_count: int = 1
    # 只跟踪“当前整合认知内容”的版本。提及次数或情绪变化不增加它。
    # 这是“事实内容”的版本号，不是被提及次数。
    # 摘要被真正整合修改时才递增；单纯重复提及或情绪变化不递增。
    revision: int = 1
    created_at: str = ""
    last_accessed_at: str = ""
    last_modified_at: str = ""
    source: str = "inferred"
    confidence: float = 0.8       #可信度


@dataclass
class MemoryRelation:
    relation_id: str
    source_concept_id: str       #来源记忆实体
    relation_type: str           #关系类型
    target_concept_id: str       #指向记忆实体
    weight: float = 1.0          #权重
    created_at: str = ""         #创建时间
    last_reinforced_at: str = "" #上次提及时间
    confidence: float = 1.0      #可信度
