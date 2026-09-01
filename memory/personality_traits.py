"""人格特质注册表（框架层）。

注册表只定义特质的含义和计算尺度，不保存某个角色的当前分数。
角色的出生值放在 personality_seed，经历证据放在 trait_evidence。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TraitDefinition:
    trait_id: str
    display_name: str
    description: str
    low_description: str
    high_description: str
    trait_delta_scale: float


TRAIT_REGISTRY: dict[str, TraitDefinition] = {
    "openness": TraitDefinition(
        "openness", "开放性", "接受新方法、探索和不确定性的倾向",
        "倾向使用已验证的方法，在不确定时较保守",
        "愿意探索新方案，并接受合理的不确定性",
        6.0,
    ),
    "conscientiousness": TraitDefinition(
        "conscientiousness", "尽责性", "检查、完成承诺和处理细节的倾向",
        "优先完成核心任务，可能跳过非关键检查",
        "倾向完成承诺、检查细节并主动验证结果",
        4.0,
    ),
    "extraversion": TraitDefinition(
        "extraversion", "外向性", "主动询问、主动表达和社交主动性",
        "等待明确指令，必要时才主动表达",
        "会主动询问、表达观点和补充必要信息",
        5.0,
    ),
}


def get_trait_definition(trait_id: str | None) -> TraitDefinition | None:
    return TRAIT_REGISTRY.get(str(trait_id or "").strip())


def list_all_traits() -> list[TraitDefinition]:
    return list(TRAIT_REGISTRY.values())
