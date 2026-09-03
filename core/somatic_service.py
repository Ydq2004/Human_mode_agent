"""身体事件的同步编排服务。

它把裁决、LLM 评价、代码计算和 SQLite 提交串成一个小闭环。
主 Agent 与工具都调用这个入口，避免各自实现一套身体规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.somatic_appraisal import SomaticAppraisal, appraise_somatic_event
from core.somatic_simulation import (
    SomaticEventDecision,
    SomaticState,
    adjudicate_somatic_event,
    apply_somatic_appraisal,
)
from memory.somatic_store import (
    commit_somatic_transition,
    get_somatic_operation,
    get_somatic_state,
)


@dataclass(frozen=True)
class SomaticOutcome:
    status: str
    operation_id: str
    source_kind: str
    decision: SomaticEventDecision
    appraisal: SomaticAppraisal | None
    state_before: SomaticState
    state_after: SomaticState
    revision: int
    visible_reaction: str = ""
    interoception_description: str = ""
    action_description: str = ""
    error: str = ""
    duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation_id": self.operation_id,
            "source_kind": self.source_kind,
            "decision": self.decision.to_dict(),
            "appraisal": self.appraisal.to_dict() if self.appraisal else None,
            "state_before": self.state_before.to_dict(),
            "state_after": self.state_after.to_dict(),
            "revision": self.revision,
            "visible_reaction": self.visible_reaction,
            "interoception_description": self.interoception_description,
            "action_description": self.action_description,
            "error": self.error,
            "duplicate": self.duplicate,
        }


def _decision_from_dict(value: Mapping[str, Any]) -> SomaticEventDecision:
    allowed = {
        "status",
        "contact_object",
        "contact_area",
        "intensity",
        "duration",
        "uncertainty",
        "reason",
    }
    return SomaticEventDecision(**{key: value[key] for key in allowed if key in value})


def _appraisal_from_dict(value: Mapping[str, Any] | None) -> SomaticAppraisal | None:
    if not isinstance(value, Mapping):
        return None
    allowed = set(SomaticAppraisal.__dataclass_fields__)
    return SomaticAppraisal(**{key: value[key] for key in allowed if key in value})


class SomaticService:
    """绑定一个 persona 实例和一条线程的身体状态服务。"""

    def __init__(
        self,
        *,
        persona_instance_id: str,
        thread_id: str,
        llm,
        persona_context: Mapping[str, Any] | None = None,
        enabled: bool = True,
        allow_agent_initiated_actions: bool = True,
    ) -> None:
        self.persona_instance_id = str(persona_instance_id or "").strip()
        self.thread_id = str(thread_id or "").strip()
        if not self.persona_instance_id or not self.thread_id:
            raise ValueError("persona_instance_id 和 thread_id 不能为空")
        self.llm = llm
        self.persona_context = dict(persona_context or {})
        self.enabled = bool(enabled)
        self.allow_agent_initiated_actions = bool(allow_agent_initiated_actions)

    def current_state(self):
        return get_somatic_state(self.persona_instance_id, self.thread_id)

    def _restore_outcome(self, operation: Mapping[str, Any]) -> SomaticOutcome:
        result = dict(operation.get("result") or {})
        return SomaticOutcome(
            status="completed",
            operation_id=str(operation["operation_id"]),
            source_kind=str(operation["source_kind"]),
            decision=_decision_from_dict(operation.get("decision") or {"status": "uncertain"}),
            appraisal=_appraisal_from_dict(operation.get("appraisal")),
            state_before=SomaticState(operation.get("state_before") or {}),
            state_after=SomaticState(operation.get("state_after") or {}),
            revision=int(operation.get("resulting_revision") or 0),
            visible_reaction=str(result.get("visible_reaction") or ""),
            interoception_description=str(result.get("interoception_description") or ""),
            action_description=str(result.get("action_description") or ""),
            duplicate=True,
        )

    def process(
        self,
        request: Mapping[str, Any] | None,
        *,
        source_event_id: str,
        operation_id: str,
        source_kind: str,
        event_context: Mapping[str, Any] | None = None,
    ) -> SomaticOutcome:
        """同步处理一次身体事件，让反馈能进入主 Agent 的本轮思考。"""
        current = self.current_state()
        op_id = str(operation_id or "").strip()
        if not self.enabled:
            return SomaticOutcome(
                status="disabled",
                operation_id=op_id,
                source_kind=source_kind,
                decision=SomaticEventDecision("rejected", reason="当前实例未启用叙事身体能力。"),
                appraisal=None,
                state_before=current.state,
                state_after=current.state,
                revision=current.revision,
            )
        if source_kind == "agent_action" and not self.allow_agent_initiated_actions:
            return SomaticOutcome(
                status="rejected",
                operation_id=op_id,
                source_kind=source_kind,
                decision=SomaticEventDecision(
                    "rejected",
                    reason="当前角色配置不允许 Agent 主动发起身体动作。",
                ),
                appraisal=None,
                state_before=current.state,
                state_after=current.state,
                revision=current.revision,
            )
        if source_kind == "agent_action" and (
            not str((request or {}).get("action_description") or "").strip()
            or not str((request or {}).get("target") or "").strip()
        ):
            return SomaticOutcome(
                status="uncertain",
                operation_id=op_id,
                source_kind=source_kind,
                decision=SomaticEventDecision(
                    "uncertain",
                    reason="主动身体动作缺少动作描述或目标。",
                ),
                appraisal=None,
                state_before=current.state,
                state_after=current.state,
                revision=current.revision,
            )

        existing = get_somatic_operation(op_id)
        if existing is not None:
            return self._restore_outcome(existing)

        decision = adjudicate_somatic_event(request)
        if decision.status != "occurred":
            return SomaticOutcome(
                status=decision.status,
                operation_id=op_id,
                source_kind=source_kind,
                decision=decision,
                appraisal=None,
                state_before=current.state,
                state_after=current.state,
                revision=current.revision,
            )

        appraisal = appraise_somatic_event(
            llm=self.llm,
            decision=decision,
            state=current.state,
            event_context=dict(event_context or request or {}),
            persona_context=self.persona_context,
        )
        if appraisal.status != "normal":
            return SomaticOutcome(
                status="failed",
                operation_id=op_id,
                source_kind=source_kind,
                decision=decision,
                appraisal=appraisal,
                state_before=current.state,
                state_after=current.state,
                revision=current.revision,
                error=appraisal.error,
            )

        next_state, _changes = apply_somatic_appraisal(
            current.state,
            appraisal.to_dict(),
        )
        result_payload = {
            "visible_reaction": appraisal.visible_reaction,
            "interoception_description": appraisal.interoception_description,
            "action_description": str((request or {}).get("action_description") or "").strip(),
        }
        committed = commit_somatic_transition(
            operation_id=op_id,
            persona_instance_id=self.persona_instance_id,
            thread_id=self.thread_id,
            source_event_id=source_event_id,
            source_kind=source_kind,
            expected_revision=current.revision,
            state_after=next_state,
            decision=decision.to_dict(),
            appraisal=appraisal.to_dict(),
            result=result_payload,
        )
        if committed.status != "committed":
            return SomaticOutcome(
                status="revision_conflict",
                operation_id=op_id,
                source_kind=source_kind,
                decision=decision,
                appraisal=appraisal,
                state_before=current.state,
                state_after=committed.record.state,
                revision=committed.record.revision,
                error="身体状态已被其他事件更新，本次旧状态计算未提交。",
            )

        operation = committed.operation
        return SomaticOutcome(
            status="completed",
            operation_id=op_id,
            source_kind=source_kind,
            decision=decision,
            appraisal=appraisal,
            state_before=SomaticState(operation["state_before"]),
            state_after=SomaticState(operation["state_after"]),
            revision=committed.record.revision,
            visible_reaction=appraisal.visible_reaction,
            interoception_description=appraisal.interoception_description,
            action_description=result_payload["action_description"],
            duplicate=committed.duplicate,
        )


def format_somatic_context(outcomes: list[SomaticOutcome]) -> str:
    """把本轮已发生的身体反馈交给主 Agent，不暴露内部计算提示词。"""
    completed = [item for item in outcomes if item.status == "completed"]
    if not completed:
        return ""
    lines = [
        "【本轮身体反馈】",
        "以下是身体模块已经完成的角色世界反馈。身体反应不代表同意、好感或关系变化。",
    ]
    for item in completed:
        lines.append(f"- 内感受：{item.interoception_description}")
        lines.append(f"- 可见反应：{item.visible_reaction}")
    return "\n".join(lines) + "\n\n"


__all__ = ["SomaticOutcome", "SomaticService", "format_somatic_context"]
