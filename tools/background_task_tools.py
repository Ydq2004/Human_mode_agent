"""主 Agent 使用的高级模型与后台任务工具。

工具只负责校验请求、创建任务或读取状态；真正的审核执行由
``BackgroundTaskWorker`` 完成，避免把耗时工作阻塞在主 Agent 回合里。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from langchain_core.tools import tool

from config import ADVANCED_CONSULT_MAX_CALLS_PER_TURN, REVIEW_MAX_FILES
from memory import background_task_store as store
from tools.file_access_policy import check_path


@dataclass
class _Runtime:
    worker: Any
    event_bus: Any
    llm: Any
    persona_instance_id: str
    thread_id: str
    source_event_id: str = ""
    consult_count: int = 0


_RUNTIME: ContextVar[_Runtime | None] = ContextVar("background_task_runtime", default=None)


@contextmanager
def background_task_context(*, worker, event_bus, llm, persona_instance_id: str, thread_id: str, source_event_id: str = "") -> Iterator[None]:
    token = _RUNTIME.set(_Runtime(worker, event_bus, llm, str(persona_instance_id or ""), str(thread_id or ""), str(source_event_id or "")))
    try:
        yield
    finally:
        _RUNTIME.reset(token)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


@tool
def consult_advanced_model(question: str, context: str = "") -> str:
    """就当前问题向高级模型请求一次同步建议。

    这是只读咨询，不执行文件、命令、数据库或设备操作；需要较长时间的代码审核
    应使用 ``start_code_review``，再用 ``get_background_task_status`` 查看结果。
    """
    runtime = _RUNTIME.get()
    if runtime is None or runtime.llm is None:
        return _json({"operation": "consult_advanced_model", "status": "unavailable", "reason": "高级模型未配置或当前没有任务上下文"})
    if runtime.consult_count >= ADVANCED_CONSULT_MAX_CALLS_PER_TURN:
        return _json({"operation": "consult_advanced_model", "status": "call_limit_reached", "limit": ADVANCED_CONSULT_MAX_CALLS_PER_TURN})
    runtime.consult_count += 1
    from core.advanced_model_service import run_consultation
    try:
        task = {"request": {"question": str(question or "").strip(), "context": str(context or "").strip()}}
        result = run_consultation(task, runtime.llm)
    except Exception as exc:
        return _json({"operation": "consult_advanced_model", "status": "failed", "error": type(exc).__name__, "reason": str(exc)})
    return _json({"operation": "consult_advanced_model", "status": "succeeded", **result})


@tool
def start_code_review(focus: str, paths: list[str], idempotency_key: str = "") -> str:
    """启动一次后台只读代码审核，立即返回 task_id。

    ``paths`` 必须是项目内需要审核的文件或目录绝对路径列表；审核器只能读取这些
    范围与全局读取白名单的交集，不能写文件、执行命令或修改记忆。
    """
    runtime = _RUNTIME.get()
    if runtime is None or runtime.worker is None or runtime.llm is None:
        return _json({"operation": "start_code_review", "status": "unavailable", "reason": "高级模型未配置或当前没有后台任务上下文"})
    if not isinstance(paths, list) or not paths or len(paths) > REVIEW_MAX_FILES:
        return _json({"operation": "start_code_review", "status": "invalid_request", "reason": f"paths 必须是 1 到 {REVIEW_MAX_FILES} 项"})
    clean_paths = [str(item).strip() for item in paths if str(item).strip()]
    if len(clean_paths) != len(paths):
        return _json({"operation": "start_code_review", "status": "invalid_request", "reason": "paths 不能包含空路径"})
    for path in clean_paths:
        # 任务范围必须先落在全局读取白名单内；审核器不能借任务参数扩大权限。
        directory_check = check_path(path, "list")
        file_check = check_path(path, "read")
        if directory_check.status != "allow" and file_check.status != "allow":
            return _json({"operation": "start_code_review", "status": "denied", "path": path, "reason": "审核路径不在全局读取白名单内或属于敏感路径"})
    key = str(idempotency_key or "").strip() or None
    existing = store.get_task_by_idempotency_key(key) if key else None
    if existing is not None:
        return _json({"operation": "start_code_review", "status": existing.get("status"), "task_id": existing.get("task_id"), "task_type": existing.get("task_type"), "message": "已存在相同幂等键的审核任务"})
    task = store.create_task(
        task_type="code_review", execution_mode="async",
        request={"focus": str(focus or "").strip() or "检查实现正确性、边界和测试覆盖"},
        scope={"paths": clean_paths},
        persona_instance_id=runtime.persona_instance_id,
        thread_id=runtime.thread_id,
        source_event_id=runtime.source_event_id,
        idempotency_key=key,
        max_attempts=2,
    )
    runtime.worker.submit(task["task_id"])
    return _json({"operation": "start_code_review", "status": task["status"], "task_id": task["task_id"], "task_type": task["task_type"], "message": "审核已进入后台；完成后可用 get_background_task_status 查询"})


@tool
def get_background_task_status(task_id: str, include_result: bool = True) -> str:
    """读取后台任务状态、错误和（完成后）审核报告。"""
    task = store.get_task(str(task_id or "").strip(), include_result=bool(include_result))
    if task is None:
        return _json({"operation": "get_background_task_status", "status": "not_found", "task_id": str(task_id or "").strip()})
    runtime = _RUNTIME.get()
    if runtime is not None and (task.get("thread_id") != runtime.thread_id or task.get("persona_instance_id") != runtime.persona_instance_id):
        return _json({"operation": "get_background_task_status", "status": "denied", "task_id": str(task_id or "").strip(), "reason": "任务不属于当前线程或实例"})
    return _json({"operation": "get_background_task_status", **task})


@tool
def list_background_tasks(status: str = "", limit: int = 20) -> str:
    """列出当前线程的后台任务。"""
    runtime = _RUNTIME.get()
    if runtime is None:
        return _json({"operation": "list_background_tasks", "status": "unavailable", "reason": "当前没有后台任务上下文"})
    try:
        rows = store.list_tasks(persona_instance_id=runtime.persona_instance_id, thread_id=runtime.thread_id, status=str(status or "").strip() or None, limit=int(limit))
    except Exception as exc:
        return _json({"operation": "list_background_tasks", "status": "invalid_request", "reason": str(exc)})
    return _json({"operation": "list_background_tasks", "status": "succeeded", "tasks": rows})


@tool
def cancel_background_task(task_id: str) -> str:
    """请求取消一个当前线程的后台任务；运行中的任务会在安全边界停止。"""
    runtime = _RUNTIME.get()
    task = store.get_task(str(task_id or "").strip())
    if task is None:
        return _json({"operation": "cancel_background_task", "status": "not_found"})
    if runtime is not None and (task.get("thread_id") != runtime.thread_id or task.get("persona_instance_id") != runtime.persona_instance_id):
        return _json({"operation": "cancel_background_task", "status": "denied", "reason": "任务不属于当前线程或实例"})
    try:
        updated = store.request_cancel(task["task_id"])
    except Exception as exc:
        return _json({"operation": "cancel_background_task", "status": "failed", "reason": str(exc)})
    return _json({"operation": "cancel_background_task", "status": updated.get("status") if updated else "not_found", "task_id": task["task_id"]})


__all__ = ["background_task_context", "consult_advanced_model", "start_code_review", "get_background_task_status", "list_background_tasks", "cancel_background_task"]
