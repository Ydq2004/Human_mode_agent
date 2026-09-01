"""
Human-mode Agent 主入口。

CLI 只是输入适配器：
字符串 -> PerceptionEvent

主循环不再直接处理 user_input、旧 retrieved_concepts 或旧 appraisal。
"""

import json
from copy import deepcopy
from textwrap import indent
from time import perf_counter

from config import (
    COMMIT_RECOVERY_MAX_AGE_HOURS,
    DEFAULT_THREAD_ID,
    MAX_RECENT_TURN,
)

from core.model_io import (
    perception_event_to_model_message,
)

from core.agent_factory import (
    create_agent_from_persona,
    set_current_injection,
)
from core.appraisal_worker import AppraisalWorker
from core.commit_worker import CommitTask, CommitWorker
from core.vector_repair_worker import VectorRepairWorker
from core.memory_commit import MemoryCommitService
from core.context_builder import build_agent_context
from core.experience import (
    create_agent_action,
    create_experience_slice,
)
from core.perception import (
    PerceptionEvent,
    PerceptionFrame,
    create_perception_event,
)
from emotion.manager import begin_perception_event
from emotion.models import ensure_emotion_store
from memory.store_manager import (
    ensure_memory_store,
    inspect_vector_consistency,
    initialize_genesis_memory,
    repair_pending_vectors,
)
from memory.experience_store import (
    get_experience_slice,
    get_next_event_sequence,
    save_experience_slice,
)
from memory.recall_tools import recall_call_budget, recall_memory
from memory.summary_scheduler import generate_missing_period_summaries
from memory import commit_store
from memory.personality_store import ensure_personality_seed_from_persona


_DEBUG_SEPARATOR = "-" * 72
_TIMING_LABELS = {
    "understanding_seconds": "感知理解",
    "retrieval_seconds": "认知检索",
    "main_agent_seconds": "主 Agent",
    "appraisal_seconds": "经验评价 LLM",
    "rules_seconds": "规则计算",
}


def _debug_json_default(value):
    """把项目结构转换成调试输出可用的 JSON，不改变源对象。"""
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return str(value)


def _print_debug_header(title: str, *, reference: str = "") -> None:
    """开始一个独立调试区块，避免不同阶段的内容粘在一起。"""
    print(f"\n{_DEBUG_SEPARATOR}")
    print(f"调试 | {title}")
    if reference:
        print(f"标识 | {reference}")
    print(_DEBUG_SEPARATOR)


def _print_debug_value(title: str, value) -> None:
    """以缩进 JSON 显示一个结构化调试区域。"""
    print(f"\n[{title}]")

    if value is None or value == [] or value == {}:
        print("  （无）")
        return

    rendered = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        default=_debug_json_default,
    )
    print(indent(rendered, "  "))


def _print_debug_timings(timings: dict | None) -> None:
    """逐行显示各阶段耗时；未知计时项仍保留原字段名。"""
    print("\n[耗时]")

    if not timings:
        print("  （无）")
        return

    for key, value in timings.items():
        label = _TIMING_LABELS.get(key, key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            print(f"  - {label}：{value:.3f} 秒")
        else:
            print(f"  - {label}：{value}")


def _compact_memory_refs(refs: list[dict] | None) -> list[dict]:
    """只显示自然唤起认知的定位信息，避免把长摘要重复刷到终端。"""
    compact = []
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        compact.append({
            "concept_id": ref.get("concept_id"),
            "canonical_name": ref.get("canonical_name"),
            "memory_type": ref.get("memory_type"),
            "emotion_label": ref.get("emotion_label"),
            "emotion_score": ref.get("emotion_score"),
            "retrieval_sources": ref.get("retrieval_sources") or [],
        })
    return compact


def _compact_experience_debug(experience_slice) -> dict:
    """显示 ExperienceSlice 的保存确认，不展开其中的大段工具结果。"""
    event = experience_slice.perception_event
    actions = list(experience_slice.response_or_actions or ())
    observations = list(experience_slice.observations or ())
    return {
        "saved": True,
        "slice_id": experience_slice.slice_id,
        "event_id": event.event_id,
        "occurred_at": event.occurred_at,
        "content_length": len(event.content),
        "action_count": len(actions),
        "tool_call_count": sum(
            action.action_type == "tool_call" for action in actions
        ),
        "observation_count": len(observations),
        "tool_result_count": sum(
            observation.modality == "memory_recall_result"
            for observation in observations
        ),
    }

def _build_persona_context(persona: dict) -> dict:
    """
    为感知理解器提供必要的角色上下文。

    这些信息只帮助理解事件，不会被重复拼进 Agent 的动态记忆文本。
    """
    keys = (
        "agent_name",
        "self_terms",
        "user_role",
        "relationship",
        "personality",
        "goals",
        "values",
        "boundaries",
        "obedience_rule",
        "knowledge_boundary",
        "initiative",
    )

    context = {
        key: persona[key]
        for key in keys
        if persona.get(key)
    }

    expression_preferences = persona.get(
        "expression_preferences"
    )
    if (
        "initiative" not in context
        and isinstance(expression_preferences, dict)
        and expression_preferences.get("initiative")
    ):
        context["initiative"] = expression_preferences[
            "initiative"
        ]

    return context

def _build_capability_snapshot(tools: list | None) -> dict:
    """
    创建当前 Agent 的能力快照。

    这里记录的是“当前确实拥有的能力”，
    不能根据角色名字或模型知识自行猜测。
    """
    tool_names = []

    for tool in tools or []:
        tool_name = getattr(tool, "name", None)

        if not tool_name:
            tool_name = getattr(tool, "__name__", None)

        if not tool_name:
            tool_name = type(tool).__name__

        tool_names.append(str(tool_name))

    return {
        "owner": "agent",
        "registered_tools": tool_names,

        # 当前 Phase 0 没有真实身体或设备控制工具。
        "physical_action": "没有真实的物理身体,如果需要请输出动作描写进行模拟,使用时需要把动作描写放进括号里比如:(抬头看向天空)",
        "device_control": False,
        "environment_observation": False,
        "long_term_memory_control": {
            "managed_by": "background_framework",
            "agent_can_write": False,
            "agent_can_delete": False,
            "result_available_during_response": False,
        },
        "memory_access": {
            # 自动唤起只是第一反应，不是完整查库。
            "automatic_activation": {
                "available": True,
                "exhaustive": False,
            },
            # 这是只读回忆工具；写入、修改和删除仍由后台框架负责。
            "deliberate_recall": {
                "available": True,
                "tool": "recall_memory",
            },
        },
    }


def _extract_tool_interactions(messages: list) -> tuple[list, list[PerceptionEvent]]:
    """把本轮 Agent 的工具调用和工具结果转成可追溯经历记录。

    LangChain 会把工具调用放在 AIMessage.tool_calls，把返回值放在
    ToolMessage.content。这里不改变模型消息，只为 ExperienceSlice 复制
    一份记录；工具结果仍然只是观察，不会自动变成长期认知。
    """
    actions = []
    observations = []

    for message in messages or []:
        tool_calls = getattr(message, "tool_calls", None) or []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            call_name = str(call.get("name") or "tool").strip()
            call_payload = {
                "name": call_name,
                "args": call.get("args") or {},
                "tool_call_id": call.get("id") or call.get("tool_call_id"),
            }
            actions.append(
                create_agent_action(
                    "tool_call",
                    json.dumps(call_payload, ensure_ascii=False, default=str),
                    metadata={"tool_name": call_name},
                )
            )

        if getattr(message, "type", "") != "tool":
            continue
        content = getattr(message, "content", "")
        if isinstance(content, str):
            rendered = content.strip()
        else:
            rendered = json.dumps(content, ensure_ascii=False, default=str)
        if not rendered:
            rendered = "工具返回空结果"
        observations.append(
            create_perception_event(
                source="tool",
                modality="memory_recall_result",
                content=rendered,
                metadata={
                    "tool_name": str(getattr(message, "name", "") or ""),
                    "tool_call_id": str(
                        getattr(message, "tool_call_id", "") or ""
                    ),
                },
            )
        )

    return actions, observations

def _build_perception_frame(
    event: PerceptionEvent,
    thread_id: str,
    recent_context: list[str],
    persona: dict,
    capability_snapshot:dict|None=None,
) -> PerceptionFrame:
    """
    在事件边界创建 PerceptionFrame。

    情绪状态只在这里读取一次。
    同一事件后续的理解、检索和回应都使用这份快照。
    """
    if MAX_RECENT_TURN > 0:
        working_context = "\n".join(
            recent_context[-MAX_RECENT_TURN:]
        )
    else:
        working_context = ""

    # 事件边界只推进一次状态。
    raw_state_snapshot = begin_perception_event(thread_id)

    # 明确 mood / energy 属于 Agent 自身。
    # 这样模型不会把 Agent 的 energy 误认为用户的精力。
    state_snapshot = {
        "owner": "agent",
        **raw_state_snapshot,
    }

    return PerceptionFrame(
        perception_event=event,
        working_context=working_context,
        state_snapshot=state_snapshot,
         capability_snapshot=(
            capability_snapshot
            or _build_capability_snapshot([])
        ),
        persona_context=_build_persona_context(persona),
    )



def _shorten(text: str, limit: int = 240) -> str:
    """只为下一轮工作上下文保留短摘要，避免无限累积。"""
    text = str(text or "").strip()

    if len(text) <= limit:
        return text

    return text[:limit] + "..."


def _build_preceding_context(recent_context: list[str]) -> dict:
    """
    把本轮之前的有限上下文冻结成 ExperienceSlice 的结构化输入。

    这里只保留已经发生过的短摘要，不把当前 Persona 或本轮解释塞进经历事实。
    """
    recent_events = [
        {
            "summary": _shorten(item),
            "source": "recent_context",
        }
        for item in recent_context[-MAX_RECENT_TURN:]
    ] if MAX_RECENT_TURN > 0 else []

    return {
        "recent_relevant_events": recent_events,
    }


def _working_context_from_experience(
    experience_slice,
) -> str:
    """
    从上一段经验生成下一轮可用的短时上下文。

    这不是长期记忆，也不是回合评价，只是帮助下一轮理解当前连续互动。
    """
    event = experience_slice.perception_event

    response_texts = [
        action.content
        for action in experience_slice.response_or_actions
        if action.action_type == "visible_response"
    ]

    response_text = "；".join(response_texts)

    return (
        f"最近感知（{event.source}/{event.modality}）："
        f"{_shorten(event.content)}\n"
        f"最近回应：{_shorten(response_text)}"
    )


def process_perception_event(
    event: PerceptionEvent,
    *,
    agent,
    agent_config: dict,
    understanding_llm,
    retry_understanding_llm,
    appraisal_worker: AppraisalWorker,
    thread_id: str,
    persona: dict,
    recent_context: list[str],
    capability_snapshot: dict | None = None,
    event_sequence: int | None = None,
) -> dict:
    """
    处理一个感知事件，返回本轮上下文和 ExperienceSlice。

    这是框架层的无写入主循环闭环：
    - 创建当前观察框
    - 生成感知理解
    - 检索长期认知
    - 调用 Agent
    - 记录实际回应
    - 把冻结的 ExperienceSlice 提交给后台 worker

    本函数不执行：
    - mood / energy 状态写入
    - resolver / judge
    - 任何长期记忆写入
    """
    # 防止上一个事件的动态注入泄漏到当前事件。
    set_current_injection("")
    frame = _build_perception_frame(
        event=event,
        thread_id=thread_id,
        recent_context=recent_context,
        persona=persona,
        capability_snapshot=capability_snapshot,
    )
    context_result = build_agent_context(
        perception_frame=frame,
        understanding_llm=understanding_llm,
        retry_understanding_llm=retry_understanding_llm,
    )

    # 动态上下文通过 Agent middleware 注入。
    # 用户/环境事件本身仍作为本轮真实消息传给 Agent。
    set_current_injection(
        context_result["injection_text"]
    )

    visible_reply = ""
    started = perf_counter()

    try:
        # PerceptionEvent 是框架里的真实感知事件；
        # 这里仅把它转换成模型供应商能够接收的消息格式。
        model_message = perception_event_to_model_message(event)
        # 主 Agent 的主动回忆次数属于“单轮”运行状态，
        # 必须在本次 invoke 外层建立预算，不能使用跨轮累加的全局计数。
        with recall_call_budget():
            result = agent.invoke(
                {"messages": [model_message]},
                config=agent_config,
            )

        final_message = result["messages"][-1]
        visible_reply = final_message.content
        if not isinstance(visible_reply, str):
            raise TypeError(
                "主 Agent 的可见回复必须是字符串，"
                f"实际类型为 {type(visible_reply).__name__}"
            )

        visible_reply = visible_reply.strip()
        if not visible_reply:
            raise RuntimeError("主 Agent 返回了空的可见回复")

    finally:
        # 动态注入只属于本次模型调用，不能泄漏到下一个事件。
        set_current_injection("")

    main_agent_seconds = perf_counter() - started

    visible_action = create_agent_action(
        action_type="visible_response",
        content=visible_reply,
    )

    # 工具调用和返回值是本轮真实发生的运行记录，保存为行动/观察，
    # 这样以后能知道 Agent 是否查过某段经历，而不是只看它的文字。
    tool_actions, observations = _extract_tool_interactions(
        result.get("messages", [])
    )

    experience_slice = create_experience_slice(
        perception_event=event,
        perception_understanding=(
            context_result.get("perception_understanding")
            or {}
        ),
        activated_memory_refs=(
            context_result.get("activated_memory_refs")
            or []
        ),
        response_or_actions=[visible_action, *tool_actions],
        observations=observations,
        state_snapshot=frame.state_snapshot,
        capability_snapshot=frame.capability_snapshot,
        memory_activation_state=(
            context_result.get("memory_activation_state")
            or {}
        ),
        preceding_context=_build_preceding_context(recent_context),
    )

    # 先保存“已经发生的完整经历”，再把它交给后台评价。
    # 评价可能失败或进程随后退出，但原始经历不能因为评价失败而消失。
    # save_experience_slice 采用稳定 slice_id + 内容哈希，重复提交是幂等的。
    save_experience_slice(
        experience_slice,
        thread_id=thread_id,
        event_sequence=event_sequence,
    )

    mood_reactivity = persona[
      "emotion_profile"
    ]["mood_reactivity"]

    # 在真正提交后台评价前先保存可重建输入。进程若此处之后崩溃，
    # 启动恢复可以用同一 ExperienceSlice 重新评价，而不是凭 job_id 猜测。
    if event_sequence is not None:
        commit_store.record_appraisal_pending(
            job_id=f"appraisal_{experience_slice.slice_id}",
            experience_slice_id=experience_slice.slice_id,
            event_id=event.event_id,
            event_sequence=int(event_sequence),
            thread_id=thread_id,
            persona_context=dict(frame.persona_context),
            mood_reactivity=float(mood_reactivity),
        )

    appraisal_job_id = appraisal_worker.submit(
        experience=experience_slice,
        persona_context=dict(frame.persona_context),
        mood_reactivity=mood_reactivity,
        event_sequence=event_sequence,
        thread_id=thread_id,
    )

    timings = dict(context_result.get("timings") or {})
    timings["main_agent_seconds"] = main_agent_seconds

    return {
        "perception_frame": frame,
        "context_result": context_result,
        "visible_reply": visible_reply,
        "experience_slice": experience_slice,
        "appraisal_job_id": appraisal_job_id,
        "appraisal_job": appraisal_worker.snapshot(appraisal_job_id),
        "event_sequence": event_sequence,
        "timings": timings,
        # Persona 是 appraisal 的独立输入，不写进 ExperienceSlice 经历事实。
        "persona_context": dict(frame.persona_context),
    }


def _print_frontend_debug(result: dict) -> None:
    """按处理阶段展示本轮前台结果，不改变任何业务数据。"""
    context_result = result["context_result"]
    experience_slice = result["experience_slice"]
    appraisal_job = result.get("appraisal_job") or {}
    event_id = experience_slice.perception_event.event_id

    _print_debug_header(
        "前台处理完成",
        reference=f"event_id={event_id}",
    )
    _print_debug_timings(result.get("timings"))
    _print_debug_value(
        "感知理解",
        context_result.get("perception_understanding"),
    )

    memory_refs = context_result.get("activated_memory_refs") or []
    _print_debug_value(
        f"本轮自然浮现的认知（{len(memory_refs)} 条）",
        _compact_memory_refs(memory_refs),
    )
    _print_debug_value(
        "认知唤起状态",
        context_result.get("memory_activation_state"),
    )
    _print_debug_value(
        "ExperienceSlice（已保存，终端摘要）",
        _compact_experience_debug(experience_slice),
    )

    job_view = {
        key: appraisal_job.get(key)
        for key in (
            "status",
            "job_id",
            "event_id",
            "experience_slice_id",
            "submitted_at",
            "completed_at",
            "error",
        )
        if appraisal_job.get(key) is not None
    }
    _print_debug_value("后台评价任务", job_view)


def _print_appraisal_job(job: dict) -> None:
    """CLI 只在事件边界打印后台结果，worker 自身不碰终端。"""
    _print_debug_header(
        "后台评价完成",
        reference=f"event_id={job['event_id']}",
    )

    job_view = {
        key: job.get(key)
        for key in (
            "status",
            "job_id",
            "experience_slice_id",
            "submitted_at",
            "completed_at",
        )
        if job.get(key) is not None
    }
    _print_debug_value("任务状态", job_view)

    if job["status"] == "completed":
        appraisal = job["appraisal"]
        _print_debug_timings(job.get("timings"))
        _print_debug_value(
            "ExperienceAppraisal",
            appraisal.to_dict(),
        )
        _print_debug_value(
            "AppraisalEffects（提交候选）",
            job.get("effects"),
        )
        return

    _print_debug_value(
        "失败信息",
        {"error": job.get("error")},
    )


def _consume_finished_appraisal_jobs(
    appraisal_worker: AppraisalWorker,
    commit_worker: CommitWorker | None = None,
    commit_contexts: dict[str, dict] | None = None,
) -> None:
    """
    打印已完成的后台结果，并交给有序提交线程。

    `commit_worker=None` 只保留给现有的 Step 5 单元测试。正式主流程中，
    appraisal 的引用要等 commit 进入终态后才释放。
    """
    for job in appraisal_worker.drain_finished():
        try:
            _print_appraisal_job(job)
        except Exception:
            appraisal_worker.release_delivery(job["job_id"])
            raise

        if commit_worker is None:
            if not appraisal_worker.acknowledge(job["job_id"]):
                appraisal_worker.release_delivery(job["job_id"])
                raise RuntimeError(
                    f"无法释放已消费的评价任务：{job['job_id']}"
                )
            continue

        # 旧的轮询入口仍保留作显示和兜底；正常路径已经由 appraisal 的终态回调直接提交。
        # job 自带的上下文是当前主流程的来源，commit_contexts 只是兼容旧调用者。
        context = (commit_contexts or {}).get(job["job_id"], {})
        event_sequence = context.get(
            "event_sequence",
            job.get("event_sequence"),
        )
        thread_id = context.get("thread_id", job.get("thread_id"))
        if event_sequence is None or not thread_id:
            appraisal_worker.release_delivery(job["job_id"])
            raise RuntimeError(
                f"评价任务缺少提交上下文：{job['job_id']}"
            )

        # 旧调用者可能把提交上下文放在 commit_contexts，而不是 job 本身。
        # 先合并成一份快照，再写账本和提交，保证两种入口得到同样的结果。
        ledger_job = deepcopy(job)
        ledger_job["event_sequence"] = event_sequence
        ledger_job["thread_id"] = thread_id

        try:
            commit_store.record_appraisal_terminal(ledger_job)
            commit_worker.submit(CommitTask(
                job_id=job["job_id"],
                event_sequence=event_sequence,
                thread_id=thread_id,
                appraisal_job=ledger_job,
            ))
        except Exception:
            appraisal_worker.release_delivery(job["job_id"])
            raise

        if commit_contexts is not None:
            commit_contexts.pop(job["job_id"], None)


def _print_commit_job(job: dict) -> None:
    """展示 Step 6 的真实提交结果。"""
    _print_debug_header(
        "有序提交完成",
        reference=(
            f"job_id={job['job_id']} | "
            f"event_sequence={job['event_sequence']}"
        ),
    )
    _print_debug_value("提交状态", {
        key: job.get(key)
        for key in (
            "status",
            "job_id",
            "event_sequence",
            "submitted_at",
            "completed_at",
            "error",
        )
        if job.get(key) is not None
    })
    _print_debug_value("写入结果", job.get("result"))


def _record_and_submit_commit(
    appraisal_job: dict,
    commit_worker: CommitWorker,
) -> None:
    """评价终态先写入持久账本，再交给有序提交线程。"""
    commit_store.record_appraisal_terminal(appraisal_job)
    commit_worker.submit(CommitTask(
        job_id=appraisal_job["job_id"],
        event_sequence=appraisal_job["event_sequence"],
        thread_id=appraisal_job["thread_id"],
        appraisal_job=deepcopy(appraisal_job),
    ))


def _rebuild_recovered_commit_task(row: dict) -> CommitTask | None:
    """从账本和不可变经历重建一个可提交任务；缺数据时安全跳过。"""
    appraisal = row.get("appraisal_json")
    effects = row.get("effects_json")
    if not isinstance(appraisal, dict) or not isinstance(effects, dict):
        return None

    stored = get_experience_slice(str(row.get("experience_slice_id") or ""))
    if stored is None:
        return None
    experience = stored.experience
    event_evidence = {
        "perception_event": experience.perception_event.to_dict(),
        "response_or_actions": [
            action.to_dict() for action in experience.response_or_actions
        ],
        "observations": [
            observation.to_dict() for observation in experience.observations
        ],
    }
    appraisal_job = {
        "job_id": row["job_id"],
        "event_id": row["event_id"],
        "experience_slice_id": row["experience_slice_id"],
        "event_sequence": row["event_sequence"],
        "thread_id": row["thread_id"],
        "status": "completed",
        "appraisal": appraisal,
        "effects": effects,
        "event_evidence": event_evidence,
        "submitted_at": row.get("submitted_at"),
        "completed_at": row.get("appraisal_completed_at"),
    }
    return CommitTask(
        job_id=str(row["job_id"]),
        event_sequence=int(row["event_sequence"]),
        thread_id=str(row["thread_id"]),
        appraisal_job=appraisal_job,
    )


def _prepare_commit_recovery(
    *,
    thread_id: str,
    next_event_sequence: int,
) -> tuple[list[CommitTask], int, set[int], list[str]]:
    """生成启动恢复计划，不执行任何写入。"""
    records = commit_store.list_commit_records(thread_id=thread_id)
    terminal_sequences = {
        int(row["event_sequence"])
        for row in records
        if row.get("commit_status") in {"committed", "commit_failed", "abandoned"}
    }
    candidates = commit_store.list_recoverable_commits(
        thread_id=thread_id,
        max_age_hours=COMMIT_RECOVERY_MAX_AGE_HOURS,
    )
    notes: list[str] = []
    tasks: list[CommitTask] = []
    seen_sequences: set[int] = set()
    for row in candidates:
        sequence = int(row["event_sequence"])
        if sequence in terminal_sequences or sequence in seen_sequences:
            continue
        task = _rebuild_recovered_commit_task(row)
        if task is None:
            reason = "无法从 ExperienceSlice 重建恢复任务"
            # 这类任务以后也无法靠重复启动自动修好。保留账本记录并
            # 明确收口为 abandoned，避免每次启动都重复尝试同一坏任务。
            commit_store.mark_commit_abandoned(row["job_id"], reason)
            terminal_sequences.add(sequence)
            notes.append(f"{row['job_id']}: {reason}，已标记 abandoned")
            continue
        seen_sequences.add(sequence)
        tasks.append(task)

    if not tasks:
        return [], next_event_sequence, terminal_sequences, notes

    # 恢复旧序号前，必须确认中间每个序号都有终态或恢复任务；否则提交线程
    # 会因未知缺口等待，宁可本次不自动恢复。
    start_sequence = min(task.event_sequence for task in tasks)
    known = terminal_sequences | seen_sequences
    missing = [
        sequence
        for sequence in range(start_sequence, next_event_sequence)
        if sequence not in known
    ]
    if missing:
        for task in tasks:
            notes.append(
                f"{task.job_id}: 事件序号存在缺口 {missing[:5]}，本次未自动恢复"
            )
        return [], next_event_sequence, terminal_sequences, notes

    return tasks, start_sequence, terminal_sequences, notes


def _prepare_appraisal_recovery(
    *,
    thread_id: str,
) -> tuple[list[dict], list[str]]:
    """读取可重建的 pending appraisal；缺少原始经历的任务明确收口。"""
    rows = commit_store.list_recoverable_appraisals(
        thread_id=thread_id,
        max_age_hours=COMMIT_RECOVERY_MAX_AGE_HOURS,
    )
    recoverable: list[dict] = []
    notes: list[str] = []
    for row in rows:
        stored = get_experience_slice(str(row.get("experience_slice_id") or ""))
        input_data = row.get("appraisal_input")
        if stored is None or not isinstance(input_data, dict):
            reason = "无法从 ExperienceSlice 或账本输入重建 appraisal"
            commit_store.mark_commit_abandoned(row["job_id"], reason)
            notes.append(f"{row['job_id']}: {reason}，已标记 abandoned")
            continue
        if not isinstance(input_data.get("persona_context"), dict):
            reason = "账本中的 persona_context 无效"
            commit_store.mark_commit_abandoned(row["job_id"], reason)
            notes.append(f"{row['job_id']}: {reason}，已标记 abandoned")
            continue
        try:
            mood_reactivity = float(input_data["mood_reactivity"])
        except (KeyError, TypeError, ValueError):
            reason = "账本中的 mood_reactivity 无效"
            commit_store.mark_commit_abandoned(row["job_id"], reason)
            notes.append(f"{row['job_id']}: {reason}，已标记 abandoned")
            continue
        recoverable.append({
            "row": row,
            "experience": stored.experience,
            "persona_context": input_data["persona_context"],
            "mood_reactivity": mood_reactivity,
        })
    return recoverable, notes


def _consume_finished_commit_jobs(
    appraisal_worker: AppraisalWorker,
    commit_worker: CommitWorker,
) -> None:
    """打印提交终态，并同时释放 commit 与对应 appraisal 的引用。"""
    for job in commit_worker.drain_finished():
        try:
            _print_commit_job(job)
        except Exception:
            commit_worker.release_delivery(job["job_id"])
            raise

        # 普通任务同时存在于 appraisal_worker；跨进程恢复任务只存在于
        # commit_worker，因此后者没有可释放的内存评价记录。
        if appraisal_worker.snapshot(job["job_id"]) is not None:
            if not appraisal_worker.acknowledge(job["job_id"]):
                commit_worker.release_delivery(job["job_id"])
                raise RuntimeError(
                    f"无法释放评价任务：{job['job_id']}"
                )
        if not commit_worker.acknowledge(job["job_id"]):
            raise RuntimeError(
                f"无法释放提交任务：{job['job_id']}"
            )


def main():
    # 存储初始化属于应用启动阶段，不能在“导入某个 Python 模块”时偷偷
    # 发生。Card_slot 是可替换实例槽，缺少 memory_db 时会在这里创建。
    ensure_emotion_store()

    # 只创建新记忆系统的结构表。
    # 不再调用旧的 Chroma-only genesis memory。
    ensure_memory_store()

    # 启动时补齐昨天、上周和上月的派生摘要。它只读 SQLite、
    # 不调用 LLM；摘要失败不能阻塞主 Agent 启动，原始经历仍然保留。
    try:
        generated_summaries = generate_missing_period_summaries(
            thread_id=DEFAULT_THREAD_ID,
        )
        if generated_summaries:
            print("情景摘要已补齐")
    except Exception as exc:
        print(f"情景摘要检查失败（不影响聊天）：{exc}")

    # 只注册只读回忆工具。长期认知的创建、更新和删除仍只能由 Step 6
    # 的后台提交链执行，Agent 不能通过工具绕过身份裁决直接写库。
    tools = [recall_memory]
    capability_snapshot = _build_capability_snapshot(tools)

    (
        agent,
        agent_config,
        persona,
        _system_prompt,
        understanding_llm,
        retry_understanding_llm,
        appraisal_llm,
    ) = create_agent_from_persona(tools=tools)

    # 角色卡只在实例首次启动时复制为出生人格；后续修改角色卡不会覆盖 seed。
    # 当前只建立人格演化的输入，不把人格快照注入主 Agent。
    ensure_personality_seed_from_persona(persona)

    thread_id = DEFAULT_THREAD_ID
    # 事件序号属于该对话线程的长期经历顺序。启动时从 SQLite 继续取号，
    # 这样重启后不会把新事件重新编号为 1。
    next_event_sequence = get_next_event_sequence(thread_id)

    # 角色卡的初始关系记忆必须先进入权威 SQLite 和 Chroma 索引，
    # 否则“需要检索长期认知”的提示没有可命中的初始实体。
    initialize_genesis_memory(persona)

    recovered_appraisals, appraisal_recovery_notes = _prepare_appraisal_recovery(
        thread_id=thread_id,
    )
    for note in appraisal_recovery_notes:
        print(f"评价恢复跳过：{note}")

    # 启动时先根据持久账本生成恢复计划。这里只恢复评价已完成、证据完整、
    # 事件序号没有缺口的任务；评价失败或资料不全的任务不会被伪造重跑。
    recovery_tasks, worker_start_sequence, processed_sequences, recovery_notes = (
        _prepare_commit_recovery(
            thread_id=thread_id,
            next_event_sequence=next_event_sequence,
        )
    )
    for note in recovery_notes:
        print(f"提交恢复跳过：{note}")

    # 两个 worker 通过回调连接：评价完成后立即把“终态快照”交给提交 worker，
    # 不必等下一次用户输入时轮询。deepcopy 让下游拿到独立数据，不依赖上游内部对象。
    commit_worker = CommitWorker(
        MemoryCommitService(identity_llm=appraisal_llm),
        ledger=commit_store,
        start_sequence=worker_start_sequence,
        processed_sequences=processed_sequences,
    )
    appraisal_worker = AppraisalWorker(
        appraisal_llm,
        on_terminal=lambda job: _record_and_submit_commit(
            job,
            commit_worker,
        ),
    )
    for item in recovered_appraisals:
        row = item["row"]
        try:
            appraisal_worker.submit(
                experience=item["experience"],
                persona_context=item["persona_context"],
                mood_reactivity=item["mood_reactivity"],
                event_sequence=int(row["event_sequence"]),
                thread_id=thread_id,
            )
            print(f"已恢复评价任务：{row['job_id']}")
        except Exception as exc:
            print(f"评价恢复提交失败：{row['job_id']}：{exc}")
    for task in recovery_tasks:
        try:
            commit_worker.submit(task)
            print(f"已恢复提交任务：{task.job_id}")
        except Exception as exc:
            # 恢复任务提交失败不能阻塞启动；原账本仍保留 waiting，方便人工处理。
            print(f"提交恢复失败：{task.job_id}：{exc}")

    # Chroma 只是可重建索引。独立线程低频检查一致性并按批次修复，
    # 不把网络/磁盘故障带进主 Agent 的前台请求。
    vector_repair_worker = VectorRepairWorker(
        inspect_vector_consistency,
        repair_pending_vectors,
    )
    agent_config["configurable"]["thread_id"] = thread_id
    exit_command = persona.get("exit_command", "退出")

    print(f"\n{'=' * 40}")
    print(f"  {persona['agent_name']} 已唤醒")
    print(f"  输入 '{exit_command}' 结束")
    print(f"{'=' * 40}\n")

    recent_context = []
    # 每个有效输入获得一个递增序号，CommitWorker 用它保证长期记忆按事件顺序提交。
    event_sequence = next_event_sequence - 1
    cancel_pending_on_shutdown = False

    try:
        while True:
            try:
                _consume_finished_appraisal_jobs(
                    appraisal_worker,
                    commit_worker,
                )
                _consume_finished_commit_jobs(
                    appraisal_worker,
                    commit_worker,
                )

                user_input = input("\n我：").strip()
                if not user_input:
                    continue

                # “退出”是 CLI 控制命令，不是 Agent 世界中的感知事件。
                # 在这里提前截获，既避免主 Agent 对它作答，也让 finally
                # 以正常关闭模式等待已经提交的后台评价和记忆写入完成。
                if user_input == exit_command:
                    print("\n💤 对话结束。")
                    break

                event_sequence += 1
                event = create_perception_event(
                    source="user",
                    modality="text",
                    content=user_input,
                )

                result = process_perception_event(
                    event,
                    agent=agent,
                    agent_config=agent_config,
                    understanding_llm=understanding_llm,
                    retry_understanding_llm=retry_understanding_llm,
                    appraisal_worker=appraisal_worker,
                    thread_id=thread_id,
                    persona=persona,
                    recent_context=recent_context,
                    capability_snapshot=capability_snapshot,
                    event_sequence=event_sequence,
                )

                experience_slice = result["experience_slice"]

                # 主回复先显示，不能再等待后台评价。
                print(f"\n🤖 {result['visible_reply']}")
                _print_frontend_debug(result)

                recent_context.append(
                    _working_context_from_experience(experience_slice)
                )
                if MAX_RECENT_TURN > 0:
                    del recent_context[:-MAX_RECENT_TURN]

            except KeyboardInterrupt:
                cancel_pending_on_shutdown = True
                print("\n\n💤 对话中断。")
                break
            except EOFError:
                cancel_pending_on_shutdown = True
                print("\n\n💤 输入流已关闭。")
                break
            except Exception as exc:
                import traceback

                cancel_pending_on_shutdown = True
                print(f"\n❌ 错误：{exc}")
                traceback.print_exc()
                break
    finally:
        # 关闭顺序：先停止接收/完成 appraisal，再把已结束结果交给 commit，
        # 最后等待 commit 完成并释放两边的任务记录。
        appraisal_worker.shutdown(
            wait=True,
            cancel_futures=cancel_pending_on_shutdown,
        )
        _consume_finished_appraisal_jobs(
            appraisal_worker,
            commit_worker,
        )
        commit_worker.shutdown(wait=True)
        _consume_finished_commit_jobs(
            appraisal_worker,
            commit_worker,
        )
        vector_repair_worker.shutdown(wait=True)

if __name__ == "__main__":
    main()
