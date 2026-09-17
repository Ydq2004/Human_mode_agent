"""受限本地命令执行工具。

第一版只允许 Python 测试/诊断和 git 只读子命令；不经过 shell，避免复合命令和
shell 注入。后续后台进程应复用 background_task 基础设施扩展，不能放宽这里的同步边界。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from langchain_core.tools import tool
from config import PROJECT_ROOT
from tools.file_access_policy import check_path
from tools.tool_contracts import tool_result
from tools.background_task_tools import submit_background_task


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


__all__ = ["execute_command", "run_command_task"]
