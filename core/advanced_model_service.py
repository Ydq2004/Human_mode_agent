"""高级模型咨询与代码审核服务。

高级模型只产生分析结果。文件审核使用任务级只读文件上下文，不能写文件、
执行命令或修改记忆；真正的副作用仍由主 Agent 的本地工具完成。
"""

from __future__ import annotations

import json
from time import monotonic
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from config import (
    ADVANCED_LLM_CONFIG,
    ADVANCED_LLM_MAX_TOKENS,
    ADVANCED_LLM_TEMPERATURE,
    ADVANCED_LLM_TIMEOUT_SECONDS,
    REVIEW_MAX_FILES,
    REVIEW_MAX_READ_BYTES,
    REVIEW_MAX_ROUNDS,
    REVIEW_MAX_TOOL_CALLS,
)
from memory import background_task_store as task_store
from tools.file_access_policy import check_path, max_bytes


def create_advanced_llm():
    """按 config.py 创建可选高级模型；密钥缺失时返回 None。"""
    cfg = dict(ADVANCED_LLM_CONFIG)
    env_name = str(cfg.get("api_key_env") or "").strip()
    import os
    api_key = os.environ.get(env_name, "").strip() if env_name else ""
    if not api_key or not str(cfg.get("base_url") or "").strip():
        return None
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=str(cfg.get("model_name") or "").strip(),
        api_key=api_key,
        base_url=str(cfg["base_url"]).strip(),
        reasoning_effort="high",
        temperature=ADVANCED_LLM_TEMPERATURE,
        max_tokens=ADVANCED_LLM_MAX_TOKENS,
        timeout=ADVANCED_LLM_TIMEOUT_SECONDS,
        max_retries=2,
    )


def _content(response: Any) -> str:
    value = getattr(response, "content", response)
    if isinstance(value, list):
        value = "".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in value)
    return str(value or "").strip()


def _json_or_text(text: str) -> tuple[dict[str, Any], str]:
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value, "json"
    except json.JSONDecodeError:
        pass
    return {"summary": text[:12000], "findings": [], "uncertainties": ["模型未按结构化格式返回"]}, "text"


def run_consultation(task: dict[str, Any], llm) -> dict[str, Any]:
    if llm is None:
        raise RuntimeError("高级模型未配置")
    request = task.get("request") or {}
    question = str(request.get("question") or "").strip()
    context = str(request.get("context") or "").strip()
    if not question:
        raise ValueError("咨询问题不能为空")
    prompt = (
        "你是高级模型咨询器，只提供分析建议，不执行任何文件、数据库、设备或网络副作用。\n"
        "请明确区分已知信息、假设、建议和不确定性；不要声称已经执行建议。\n\n"
        f"问题：{question}\n"
        f"必要上下文：{context[:20000]}\n"
        "请返回 JSON：answer、assumptions、recommended_actions、uncertainties。"
    )
    response = llm.invoke([HumanMessage(content=prompt)])
    parsed, fmt = _json_or_text(_content(response))
    return {
        "answer": str(parsed.get("answer") or parsed.get("summary") or "").strip()[:12000],
        "assumptions": [str(x) for x in parsed.get("assumptions", []) if str(x).strip()][:20],
        "recommended_actions": [str(x) for x in parsed.get("recommended_actions", []) if str(x).strip()][:20],
        "uncertainties": [str(x) for x in parsed.get("uncertainties", []) if str(x).strip()][:20],
        "result_format": fmt,
    }


class ReviewReadContext:
    """审核任务专用的只读文件上下文，权限是全局白名单与任务范围的交集。"""

    def __init__(self, scope: dict[str, Any]):
        paths = scope.get("paths") if isinstance(scope, dict) else None
        if not isinstance(paths, list) or not paths:
            raise ValueError("审核范围必须提供 paths")
        self.allowed = [Path(str(p)).resolve(strict=False) for p in paths if str(p).strip()]
        self.files_read = 0
        self.bytes_read = 0
        self.calls = 0
        self.audit: list[dict[str, Any]] = []

    def _within_scope(self, path: Path) -> bool:
        candidate = path.resolve(strict=False)
        return any(candidate == root or root in candidate.parents for root in self.allowed)

    def list_directory(self, path: str, limit: int = 100) -> str:
        self.calls += 1
        if self.calls > REVIEW_MAX_TOOL_CALLS:
            return json.dumps({"status": "call_limit_reached"}, ensure_ascii=False)
        check = check_path(path, "list")
        target = Path(check.resolved_path) if check.status == "allow" else None
        if target is None or not self._within_scope(target) or not target.is_dir():
            return json.dumps({"status": "denied", "reason": "目录不在本次审核范围或读取白名单内"}, ensure_ascii=False)
        entries = []
        for item in target.iterdir():
            if len(entries) >= min(max(int(limit), 1), 200):
                break
            if not self._within_scope(item):
                continue
            item_check = check_path(str(item), "read")
            if item_check.status == "allow":
                entries.append({"name": item.name, "type": "directory" if item.is_dir() else "file", "path": str(item.resolve())})
        return json.dumps({"status": "success", "path": str(target), "entries": entries}, ensure_ascii=False)

    def read_file(self, path: str) -> str:
        self.calls += 1
        if self.calls > REVIEW_MAX_TOOL_CALLS:
            return json.dumps({"status": "call_limit_reached"}, ensure_ascii=False)
        check = check_path(path, "read")
        target = Path(check.resolved_path) if check.status == "allow" else None
        if target is None or not self._within_scope(target) or not target.is_file():
            return json.dumps({"status": "denied", "reason": "文件不在本次审核范围或读取白名单内"}, ensure_ascii=False)
        if self.files_read >= REVIEW_MAX_FILES:
            return json.dumps({"status": "file_limit_reached"}, ensure_ascii=False)
        try:
            size = target.stat().st_size
            limit = min(max_bytes("read"), REVIEW_MAX_READ_BYTES - self.bytes_read)
            if size > limit:
                return json.dumps({"status": "size_limit", "path": str(target)}, ensure_ascii=False)
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return json.dumps({"status": "error", "path": str(target), "reason": str(exc)}, ensure_ascii=False)
        import hashlib
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.files_read += 1
        self.bytes_read += len(content.encode("utf-8"))
        self.audit.append({"path": str(target), "bytes": len(content.encode("utf-8")), "sha256": digest})
        return json.dumps({"status": "success", "path": str(target), "sha256": digest, "content": content}, ensure_ascii=False)


def run_code_review(task: dict[str, Any], llm) -> dict[str, Any]:
    if llm is None:
        raise RuntimeError("高级模型未配置")
    request = task.get("request") or {}
    scope = task.get("scope") or {}
    context = ReviewReadContext(scope)
    timeout_seconds = float((request.get("timeout_seconds") or 0))
    deadline = monotonic() + timeout_seconds if timeout_seconds > 0 else None
    list_tool = StructuredTool.from_function(context.list_directory, name="review_list_directory", description="列出审核范围内的目录")
    read_tool = StructuredTool.from_function(context.read_file, name="review_read_file", description="读取审核范围内的 UTF-8 文本文件")
    model = llm.bind_tools([list_tool, read_tool]) if hasattr(llm, "bind_tools") else llm
    messages = [
        SystemMessage(content=(
            "你是代码审核器。只能使用提供的 review_list_directory 和 review_read_file 只读工具。"
            "不得写文件、执行命令或把未读取的内容当成事实。审核结论必须包含证据、影响、建议和不确定性。"
        )),
        HumanMessage(content=(
            f"审核重点：{str(request.get('focus') or '检查实现正确性、边界和测试覆盖')[:2000]}\n"
            f"审核范围：{json.dumps(scope, ensure_ascii=False)}\n"
            "先按需列目录和读取文件，最后返回 JSON：summary、findings、files_examined、uncertainties。"
        )),
    ]
    for _ in range(REVIEW_MAX_ROUNDS):
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError(f"审核任务超过 {timeout_seconds:g} 秒总时限")
        response = model.invoke(messages)
        calls = getattr(response, "tool_calls", None) or []
        if not calls:
            parsed, fmt = _json_or_text(_content(response))
            parsed["result_format"] = fmt
            parsed["files_examined"] = context.audit
            parsed["audit"] = {"files_read": context.files_read, "bytes_read": context.bytes_read, "tool_calls": context.calls}
            return parsed
        messages.append(response)
        for call in calls[:REVIEW_MAX_TOOL_CALLS]:
            if deadline is not None and monotonic() >= deadline:
                raise TimeoutError(f"审核任务超过 {timeout_seconds:g} 秒总时限")
            name = str(call.get("name") or "")
            args = call.get("args") or {}
            if name == "review_list_directory":
                output = context.list_directory(str(args.get("path") or ""), int(args.get("limit") or 100))
            elif name == "review_read_file":
                output = context.read_file(str(args.get("path") or ""))
            else:
                output = json.dumps({"status": "denied", "reason": "审核器不允许此工具"}, ensure_ascii=False)
            messages.append(ToolMessage(content=output, tool_call_id=str(call.get("id") or "review-call")))
    raise TimeoutError("审核模型超过最大交互轮数")


__all__ = ["create_advanced_llm", "run_consultation", "run_code_review", "ReviewReadContext"]
