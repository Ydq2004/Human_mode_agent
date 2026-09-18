"""受限本地命令执行工具。

第一版只允许 Python 测试/诊断和 git 只读子命令；不经过 shell，避免复合命令和
shell 注入。后续后台进程应复用 background_task 基础设施扩展，不能放宽这里的同步边界。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import threading
from uuid import uuid4
from pathlib import Path
from langchain_core.tools import tool
from config import PROJECT_ROOT, CARD_SLOT_ROOT
from tools.file_access_policy import check_path
from tools.tool_contracts import tool_result
from tools.background_task_tools import submit_background_task
from memory import background_task_store as task_store

_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESSES: dict[str, subprocess.Popen] = {}


def _allowed(argv: list[str]) -> bool:
    if not argv: return False
    executable = Path(argv[0]).name.lower()
    if executable in {"python", "python.exe", Path(sys.executable).name.lower()}:
        return len(argv) >= 2 and argv[1] in {"-m", "--version"} and (len(argv) < 3 or argv[2] == "unittest")
    if executable == "git.exe" or executable == "git":
        if len(argv) < 2 or argv[1] not in {"status", "diff", "log"}:
            return False
        # 只读 Git 子命令仍可能通过选项写文件或执行外部程序。
        dangerous = {"--output", "--ext-diff", "--textconv", "--no-index"}
        return not any(item in dangerous or item.startswith("--output=") for item in argv[2:])
    return False


def run_command_task(task: dict) -> dict:
    request = task.get("request") or {}
    payload = _run_command(request["argv"], request["cwd"], request["timeout_seconds"], request["max_output_chars"])
    return {"summary": "命令执行完成", "command": payload}


def run_process_task(task: dict) -> dict:
    """后台运行一个受限命令，并把输出写入实例目录中的临时日志。"""
    request = task.get("request") or {}
    argv = request["argv"]
    cwd = request["cwd"]
    max_output_chars = int(request["max_output_chars"])
    timeout_seconds = float(request.get("timeout_seconds") or 0)
    output_path = Path(request["output_path"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={k: v for k, v in os.environ.items() if k in {"PATH", "SystemRoot", "PYTHONPATH"}},
    )
    with _PROCESS_LOCK:
        _ACTIVE_PROCESSES[task["task_id"]] = process
    try:
        with output_path.open("w", encoding="utf-8", newline="") as output:
            while True:
                latest = task_store.get_task(task["task_id"], include_result=False) or {}
                if latest.get("status") == "cancel_requested":
                    process.terminate()
                if timeout_seconds > 0 and time.monotonic() - started > timeout_seconds:
                    process.terminate()
                    raise TimeoutError(f"后台进程超过 {timeout_seconds:g} 秒限制")
                line = process.stdout.readline() if process.stdout is not None else ""
                if line:
                    output.write(line)
                    output.flush()
                elif process.poll() is not None:
                    break
                else:
                    time.sleep(0.05)
                task_store.update_heartbeat(task["task_id"], worker_id=str(task.get("worker_id") or ""))
        return {"summary": "后台进程已结束", "process": {"returncode": process.returncode, "elapsed_seconds": round(time.monotonic() - started, 3), "output_path": str(output_path), "max_output_chars": max_output_chars}}
    finally:
        with _PROCESS_LOCK:
            _ACTIVE_PROCESSES.pop(task["task_id"], None)


def request_process_cancel(task_id: str) -> bool:
    with _PROCESS_LOCK:
        process = _ACTIVE_PROCESSES.get(str(task_id or "").strip())
    if process is None:
        return False
    try:
        process.terminate()
        return True
    except OSError:
        return False


def _run_command(argv, cwd, timeout_seconds, max_output_chars):
    target = Path(cwd or PROJECT_ROOT).resolve(strict=False)
    started = time.monotonic()
    try:
        completed = subprocess.run(argv, cwd=str(target), shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds, env={k: v for k, v in os.environ.items() if k in {"PATH", "SystemRoot", "PYTHONPATH"}})
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + (exc.stderr or ""))[:max_output_chars]
        return {"status": "timeout", "error": f"命令超过 {timeout_seconds} 秒", "argv": argv, "output": output, "returncode": None, "truncated": len(output) >= max_output_chars}
    output = (completed.stdout or "") + (completed.stderr or "")
    return {"status": "success" if completed.returncode == 0 else "failed", "error": None if completed.returncode == 0 else f"退出码 {completed.returncode}", "argv": argv, "returncode": completed.returncode, "output": output[:max_output_chars], "truncated": len(output) > max_output_chars, "elapsed_seconds": round(time.monotonic() - started, 3)}


@tool
def execute_command(argv: list[str], cwd: str = "", timeout_seconds: int = 30, max_output_chars: int = 12000, background: bool = False) -> str:
    """执行受限本地命令。短命令直接执行；预计较久时设置 background=true，随后用后台任务状态工具查询或取消。"""
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item.strip() for item in argv):
        return tool_result("execute_command", "invalid_input", error="argv 必须是非空字符串数组")
    if not _allowed(argv): return tool_result("execute_command", "denied", error="命令不在当前只读白名单内")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 300:
        return tool_result("execute_command", "invalid_input", error="timeout_seconds 必须在 1 到 300 之间")
    if isinstance(max_output_chars, bool) or not isinstance(max_output_chars, int) or not 100 <= max_output_chars <= 100000:
        return tool_result("execute_command", "invalid_input", error="max_output_chars 无效")
    target = Path(cwd or PROJECT_ROOT).resolve(strict=False)
    check = check_path(str(target), "list")
    if check.status != "allow": return tool_result("execute_command", check.status, error="工作目录不在允许范围内", cwd=str(target))
    if background:
        queued = submit_background_task(task_type="command", request={"argv": argv, "cwd": str(target), "timeout_seconds": timeout_seconds, "max_output_chars": max_output_chars}, scope={"cwd": str(target)})
        return tool_result("execute_command", queued["status"], result=queued)
    payload = _run_command(argv, target, timeout_seconds, max_output_chars)
    return tool_result("execute_command", payload.pop("status"), result=payload, error=payload.pop("error", None))


@tool
def start_process(argv: list[str], cwd: str = "", timeout_seconds: int = 3600, max_output_chars: int = 50000, idempotency_key: str = "") -> str:
    """启动一个可能运行较久的受限命令，立即返回 task_id；使用进程工具查询或终止。"""
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item.strip() for item in argv):
        return tool_result("start_process", "invalid_input", error="argv 必须是非空字符串数组")
    if not _allowed(argv):
        return tool_result("start_process", "denied", error="命令不在当前只读白名单内")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 86400:
        return tool_result("start_process", "invalid_input", error="timeout_seconds 必须在 1 到 86400 之间")
    if isinstance(max_output_chars, bool) or not isinstance(max_output_chars, int) or not 100 <= max_output_chars <= 500000:
        return tool_result("start_process", "invalid_input", error="max_output_chars 无效")
    target = Path(cwd or PROJECT_ROOT).resolve(strict=False)
    check = check_path(str(target), "list")
    if check.status != "allow":
        return tool_result("start_process", check.status, error="工作目录不在允许范围内", cwd=str(target))
    key = str(idempotency_key or "").strip() or None
    existing = task_store.get_task_by_idempotency_key(key) if key else None
    if existing is not None:
        return tool_result("start_process", existing["status"], result={"task_id": existing["task_id"], "existing": True})
    task_id = f"process_{uuid4().hex}"
    output_path = Path(CARD_SLOT_ROOT) / "runtime_logs" / f"{task_id}.log"
    queued = submit_background_task(
        task_type="process",
        request={"argv": argv, "cwd": str(target), "timeout_seconds": timeout_seconds, "max_output_chars": max_output_chars, "output_path": str(output_path)},
        scope={"cwd": str(target), "output_path": str(output_path)},
        idempotency_key=key,
        max_attempts=1,
    )
    return tool_result("start_process", queued["status"], result=queued)


def _owned_process_task(task_id: str):
    runtime = __import__("tools.background_task_tools", fromlist=["_RUNTIME"])._RUNTIME.get()
    task = task_store.get_task(str(task_id or "").strip(), include_result=True)
    if task is None:
        return runtime, None, "not_found"
    if runtime is not None and (task.get("thread_id") != runtime.thread_id or task.get("persona_instance_id") != runtime.persona_instance_id):
        return runtime, task, "denied"
    if task.get("task_type") != "process":
        return runtime, task, "invalid_request"
    return runtime, task, "ok"


@tool
def get_process_status(task_id: str) -> str:
    """读取后台进程状态、退出码和错误。"""
    _runtime, task, status = _owned_process_task(task_id)
    if status != "ok":
        return tool_result("get_process_status", status, error="进程任务不存在、越权或类型不匹配", task_id=task_id)
    process = (task.get("result") or {}).get("process") or {}
    return tool_result("get_process_status", "success", result={"task_id": task["task_id"], "status": task["status"], "returncode": process.get("returncode"), "error_code": task.get("error_code"), "error_message": task.get("error_message"), "started_at": task.get("started_at"), "completed_at": task.get("completed_at")})


@tool
def get_process_output(task_id: str, max_chars: int = 20000) -> str:
    """读取后台进程输出；输出受长度限制，运行中也可读取已写入部分。"""
    _runtime, task, status = _owned_process_task(task_id)
    if status != "ok":
        return tool_result("get_process_output", status, error="进程任务不存在、越权或类型不匹配", task_id=task_id)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 100 <= max_chars <= 200000:
        return tool_result("get_process_output", "invalid_input", error="max_chars 无效")
    path = str((task.get("scope") or {}).get("output_path") or (task.get("request") or {}).get("output_path") or "")
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace") if path else ""
    except OSError as exc:
        return tool_result("get_process_output", "not_ready", error=str(exc), task_id=task_id)
    return tool_result("get_process_output", "success", result={"task_id": task_id, "output": content[-max_chars:], "truncated": len(content) > max_chars, "status": task["status"]})


@tool
def terminate_process(task_id: str) -> str:
    """请求终止当前线程的后台进程；结果仍需用 get_process_status 查询。"""
    _runtime, task, status = _owned_process_task(task_id)
    if status != "ok":
        return tool_result("terminate_process", status, error="进程任务不存在、越权或类型不匹配", task_id=task_id)
    updated = task_store.request_cancel(task_id)
    request_process_cancel(task_id)
    return tool_result("terminate_process", "success", result={"task_id": task_id, "status": (updated or {}).get("status", "cancel_requested")})


__all__ = ["execute_command", "run_command_task", "run_process_task", "start_process", "get_process_status", "get_process_output", "terminate_process"]
