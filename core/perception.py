"""
感知事件基础结构。

这是框架层：
- PerceptionEvent 记录“发生了什么”。
- PerceptionFrame 记录“此刻如何看待这个事件”。

两者必须分开，避免最近上下文、角色推测或情绪结论污染原始事件。
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import uuid4


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """
    保存事件时复制并冻结顶层字典。

    原始事件一旦进入系统，不应被后续模块悄悄改写来源、内容或元数据。
    深层数据仍应尽量保持简单 JSON 结构，方便以后记录和调试。
    """
    return MappingProxyType(deepcopy(dict(value or {})))


@dataclass(frozen=True)
class PerceptionEvent:
    """
    Agent 感知到的一次原始事件。

    source / modality 使用开放字符串，不用封闭枚举。
    当前可以是 user + text，未来也可以是 tool + action_result、
    vision + image_description、timer + observation 等。
    """

    event_id: str
    source: str
    modality: str
    content: str
    occurred_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        source = str(self.source or "").strip()
        modality = str(self.modality or "").strip()
        content = str(self.content or "").strip()

        if not source:
            raise ValueError("PerceptionEvent.source 不能为空")
        if not modality:
            raise ValueError("PerceptionEvent.modality 不能为空")
        if not content:
            raise ValueError("PerceptionEvent.content 不能为空")

        object.__setattr__(self, "source", source)
        object.__setattr__(self, "modality", modality)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        """转成普通 JSON 兼容字典，供日志或 LLM 输入边界使用。"""
        return {
            "event_id": self.event_id,
            "source": self.source,
            "modality": self.modality,
            "content": self.content,
            "occurred_at": self.occurred_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PerceptionFrame:
    """
    一次事件在“此刻”的运行时观察框。

    它不是长期事实，也不应回写到 PerceptionEvent。
    working_context、状态快照、角色相关上下文都可能随时间变化。
    """

    perception_event: PerceptionEvent
    working_context: str = ""
        # 当前 Agent 自身的状态。
    # 后续会明确标注 owner，避免把 Agent 的 energy 误认为用户的状态。
    state_snapshot: Mapping[str, Any] = field(default_factory=dict)

    # 当前真实可用的能力。
    # 例如：注册了哪些工具、是否能控制设备、是否有物理行动能力。
    capability_snapshot: Mapping[str, Any] = field(default_factory=dict)

    # 角色卡提供的身份和性格信息。
    persona_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "working_context",
            str(self.working_context or "").strip(),
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
            "persona_context",
            _freeze_mapping(self.persona_context),
        )

    def to_dict(self) -> dict[str, Any]:
        """输出本轮理解所需上下文，不改变原始事件。"""
        return {
            "perception_event": self.perception_event.to_dict(),
            "working_context": self.working_context,
            "state_snapshot": dict(self.state_snapshot),
            "capability_snapshot": dict(self.capability_snapshot),
            "persona_context": dict(self.persona_context),
        }


def create_perception_event(
    source: str,
    modality: str,
    content: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    event_id: str | None = None,
    occurred_at: str | None = None,
) -> PerceptionEvent:
    """
    创建感知事件的统一入口。

    CLI、工具回调、视觉模块都应通过这里创建事件，
    而不是各自手写 event_id 和时间。
    """
    return PerceptionEvent(
        event_id=event_id or f"evt_{uuid4().hex}",
        source=source,
        modality=modality,
        content=content,
        occurred_at=occurred_at or datetime.now().isoformat(),
        metadata=metadata or {},
    )