"""
一次经验切片的基础结构。

这是框架层：
- AgentAction 记录 Agent 实际产生的回应或行动。
- ExperienceSlice 把感知、当时理解、自动认知唤起、能力快照、行动和观察放进同一份记录。

它只记录已经发生的内容，不负责情绪裁决、记忆候选生成或写库。
"""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from core.perception import PerceptionEvent


def _freeze_mapping(
    value: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """
    复制并冻结顶层字典。

    ExperienceSlice 完成后应保持稳定，避免后续评估器修改原始记录。
    """
    if value is not None and not isinstance(value, Mapping):
        raise TypeError("需要 Mapping 或 None")

    return MappingProxyType(
        deepcopy(dict(value or {}))
    )


def _freeze_mapping_sequence(
    values: Sequence[Mapping[str, Any]] | None,
) -> tuple[Mapping[str, Any], ...]:
    """冻结结构化引用列表，并保留原有顺序。"""
    if values is None:
        return ()

    result = []

    for value in values:
        if not isinstance(value, Mapping):
            raise TypeError("结构化引用必须是 Mapping")
        result.append(_freeze_mapping(value))

    return tuple(result)


@dataclass(frozen=True)
class AgentAction:
    """
    Agent 实际执行的一次动作。

    action_type 使用开放字符串，不把框架限制成聊天：
    当前可以是 visible_response 或 internal_deliberation，
    未来也可以是 tool_call、physical_action 等。
    """

    action_id: str
    action_type: str
    content: str
    occurred_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        action_type = str(self.action_type or "").strip()
        content = str(self.content or "").strip()

        if not action_type:
            raise ValueError("AgentAction.action_type 不能为空")
        if not content:
            raise ValueError("AgentAction.content 不能为空")

        object.__setattr__(self, "action_type", action_type)
        object.__setattr__(self, "content", content)
        object.__setattr__(
            self,
            "metadata",
            _freeze_mapping(self.metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "content": self.content,
            "occurred_at": self.occurred_at,
            "metadata": deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True)
class ExperienceSlice:
    """
    一次已经完成的短时经验记录。

    observations 继续使用 PerceptionEvent：
    工具结果、环境反馈和动作反馈，本质上都是新的感知事件。
    """

    slice_id: str
    perception_event: PerceptionEvent
    perception_understanding: Mapping[str, Any]
    activated_memory_refs: tuple[Mapping[str, Any], ...]
    response_or_actions: tuple[AgentAction, ...]
    observations: tuple[PerceptionEvent, ...]
    state_snapshot: Mapping[str, Any]
    capability_snapshot: Mapping[str, Any]
    memory_activation_state: Mapping[str, Any]
    completed_at: str
    # 只记录本轮之前的有限上下文；Persona 不放进这里，避免把角色解释伪装成经历事实。
    preceding_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.perception_event, PerceptionEvent):
            raise TypeError(
                "ExperienceSlice.perception_event 必须是 PerceptionEvent"
            )

        actions = tuple(self.response_or_actions or ())
        if not all(isinstance(item, AgentAction) for item in actions):
            raise TypeError(
                "response_or_actions 中只能包含 AgentAction"
            )

        observations = tuple(self.observations or ())
        if not all(
            isinstance(item, PerceptionEvent)
            for item in observations
        ):
            raise TypeError(
                "observations 中只能包含 PerceptionEvent"
            )

        object.__setattr__(
            self,
            "perception_understanding",
            _freeze_mapping(self.perception_understanding),
        )
        object.__setattr__(
            self,
            "preceding_context",
            _freeze_mapping(self.preceding_context),
        )
        object.__setattr__(
            self,
            "activated_memory_refs",
            _freeze_mapping_sequence(self.activated_memory_refs),
        )
        object.__setattr__(
            self,
            "response_or_actions",
            actions,
        )
        object.__setattr__(
            self,
            "observations",
            observations,
        )
        object.__setattr__(
            self,
            "state_snapshot",
            _freeze_mapping(self.state_snapshot),
        )
        object.__setattr__(
            self,
            "capability_snapshot",
            _freeze_mapping(self.capability_snapshot),
        )
        object.__setattr__(
            self,
            "memory_activation_state",
            _freeze_mapping(self.memory_activation_state),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slice_id": self.slice_id,
            "perception_event": self.perception_event.to_dict(),
            "perception_understanding": deepcopy(
                dict(self.perception_understanding)
            ),
            "preceding_context": deepcopy(
                dict(self.preceding_context)
            ),
            "activated_memory_refs": [
                deepcopy(dict(ref))
                for ref in self.activated_memory_refs
            ],
            "response_or_actions": [
                action.to_dict()
                for action in self.response_or_actions
            ],
            "observations": [
                observation.to_dict()
                for observation in self.observations
            ],
            "state_snapshot": deepcopy(dict(self.state_snapshot)),
            "capability_snapshot": deepcopy(
                dict(self.capability_snapshot)
            ),
            "memory_activation_state": deepcopy(
                dict(self.memory_activation_state)
            ),
            "completed_at": self.completed_at,
        }


def create_agent_action(
    action_type: str,
    content: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> AgentAction:
    """创建动作记录，不在各入口中重复生成 id 和时间。"""
    return AgentAction(
        action_id=f"act_{uuid4().hex}",
        action_type=action_type,
        content=content,
        occurred_at=datetime.now().isoformat(),
        metadata=metadata or {},
    )


def create_experience_slice(
    perception_event: PerceptionEvent,
    perception_understanding: Mapping[str, Any] | None,
    activated_memory_refs: Sequence[Mapping[str, Any]] | None,
    response_or_actions: Sequence[AgentAction] | None,
    observations: Sequence[PerceptionEvent] | None,
    state_snapshot: Mapping[str, Any] | None,
    *,
    capability_snapshot: Mapping[str, Any] | None = None,
    memory_activation_state: Mapping[str, Any] | None = None,
    preceding_context: Mapping[str, Any] | None = None,
) -> ExperienceSlice:
    """在回应和观察完成后封装一次经验切片。"""
    return ExperienceSlice(
        slice_id=f"exp_{uuid4().hex}",
        perception_event=perception_event,
        perception_understanding=perception_understanding or {},
        preceding_context=preceding_context or {},
        activated_memory_refs=tuple(activated_memory_refs or ()),
        response_or_actions=tuple(response_or_actions or ()),
        observations=tuple(observations or ()),
        state_snapshot=state_snapshot or {},
        capability_snapshot=capability_snapshot or {},
        memory_activation_state=memory_activation_state or {},
        completed_at=datetime.now().isoformat(),
    )
