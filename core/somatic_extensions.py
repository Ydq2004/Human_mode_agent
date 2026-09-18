"""可选身体领域扩展协议。

通用身体核心只处理部位、接触、恢复和内感受。任何领域专用状态都必须通过
这个扩展接口存放在 ``extension_state`` 中，并且默认使用 DisabledSomaticExtension。
核心不会内置具体领域的部位清单、文字模板或阈值。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from core.somatic_simulation import SomaticEventDecision, SomaticState


@dataclass(frozen=True)
class SomaticExtensionResult:
    status: str = "unchanged"
    state_patch: Mapping[str, Any] | None = None
    public_events: tuple[Mapping[str, Any], ...] = ()
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"disabled", "unchanged", "updated", "failed"}:
            raise ValueError(f"未知身体扩展状态：{self.status}")
        patch = self.state_patch or {}
        if not isinstance(patch, Mapping):
            raise ValueError("身体扩展状态补丁必须是 object")
        if set(patch) - {"extension_state"}:
            raise ValueError("身体扩展只能写入 extension_state")


class SomaticExtension(Protocol):
    enabled: bool

    def apply(
        self,
        *,
        state: SomaticState,
        decision: SomaticEventDecision,
        appraisal: Mapping[str, Any],
        now: datetime | None = None,
    ) -> SomaticExtensionResult:
        """根据已经裁决的身体事件返回受限的扩展状态变化。"""


class DisabledSomaticExtension:
    """默认扩展；不创建任何领域专用状态。"""

    enabled = False

    def apply(self, **_: Any) -> SomaticExtensionResult:
        return SomaticExtensionResult(status="disabled")


def apply_extension_result(
    state: SomaticState,
    result: SomaticExtensionResult,
) -> SomaticState:
    """将扩展结果合并到隔离命名空间，拒绝覆盖通用状态。"""
    if result.status not in {"updated", "unchanged"}:
        return state
    patch = dict(result.state_patch or {})
    if not patch:
        return state
    values = state.to_dict()
    previous = values.get("extension_state")
    previous = dict(previous) if isinstance(previous, Mapping) else {}
    extension_state = patch.get("extension_state")
    if not isinstance(extension_state, Mapping):
        raise ValueError("extension_state 必须是 object")
    previous.update(dict(extension_state))
    values["extension_state"] = previous
    return SomaticState(values)


__all__ = [
    "DisabledSomaticExtension",
    "SomaticExtension",
    "SomaticExtensionResult",
    "apply_extension_result",
]
