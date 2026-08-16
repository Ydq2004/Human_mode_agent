"""
一次 ExperienceSlice 的即时回看。

这是框架层模块：
- 只整理已经完成的一次经验；
- 不计算 mood / energy；
- 不生成记忆候选；
- 不执行写库；
- 不生成对用户的回复。
"""

from dataclasses import dataclass, field
from typing import Any

from core.experience import ExperienceSlice


@dataclass(frozen=True)
class ExperienceReview:
    """一次经验切片的即时理解结果。"""

    experience_summary: str
    situated_interpretation: str
    salient_points: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    uncertainties: tuple[str, ...] = field(default_factory=tuple)
    do_not_assume: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "experience_summary": self.experience_summary,
            "situated_interpretation": self.situated_interpretation,
            "salient_points": [
                dict(item)
                for item in self.salient_points
            ],
            "uncertainties": list(self.uncertainties),
            "do_not_assume": list(self.do_not_assume),
        }


def fallback_experience_review(
    experience: ExperienceSlice,
    reason: str,
) -> ExperienceReview:
    """
    LLM 不可用时的保守回退。

    回退只保留已经发生的事实，不补充事件含义。
    """
    event = experience.perception_event

    summary = (
        f"收到 {event.source}/{event.modality} 感知："
        f"{event.content}；"
        f"Agent 产生 {len(experience.response_or_actions)} 个行动；"
        f"收到 {len(experience.observations)} 个后续观察。"
    )

    return ExperienceReview(
        experience_summary=summary,
        situated_interpretation="当前没有形成额外的情境解释。",
        salient_points=(),
        uncertainties=(reason,),
        do_not_assume=(),
    )