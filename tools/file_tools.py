"""主 Agent 使用的受控文件工具。

工具只负责文件系统操作；权限判断统一交给 ``file_access_policy``。
每个结果都是 JSON，明确返回 operation/status/path/error，便于 Agent
理解真实结果，也便于主循环写入 ExperienceSlice。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from tools.file_access_policy import (
    check_path,
    consume_authorization,
    max_bytes,
)


def _result(operation: str, status: str, path: str, **extra: Any) -> str:
    payload = {"operation": operation, "status": status, "path": path}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _checked_path(path: str, operation: str, authorization_id: str | None = None):
    check = check_path(path, operation, authorization_id=authorization_id)
    if check.status != "allow":
        return None, _result(
            operation,
            check.status,
            path,
            reason=check.reason,
            operation_id=check.operation_id,
        )
    return Path(check.resolved_path), None


@tool
def read_file(path: str) -> str:
    """读取白名单内的 UTF-8 文本文件；不会修改文件。"""
    target, error = _checked_path(path, "read")
    if error:
        return error
    try:
        if not target.is_file():
            return _result("read", "error", path, reason="目标不是文件")
        limit = max_bytes("read")
        if target.stat().st_size > limit:
            return _result("read", "error", path, reason=f"文件超过 {limit} 字节限制")
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _result("read", "error", path, reason="只支持 UTF-8 文本文件")
    except OSError as exc:
        return _result("read", "error", path, reason=str(exc))
    return _result("read", "success", path, content=content, bytes=len(content.encode("utf-8")))


@tool
def list_directory(path: str, recursive: bool = False, limit: int = 100) -> str:
    """列出白名单目录中的文件和子目录。"""
    target, error = _checked_path(path, "list")
    if error:
        return error
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
        return _result("list", "error", path, reason="limit 必须在 1 到 1000 之间")
    try:
        if not target.is_dir():
            return _result("list", "error", path, reason="目标不是目录")
        entries = []
        truncated = False
        iterator = target.rglob("*") if recursive else target.iterdir()
        for item in iterator:
            if len(entries) >= limit:
                truncated = True
                break
            # 敏感文件由 read/list 的统一策略拒绝，不在目录结果中泄露。
            check = check_path(str(item), "read")
            if check.status != "allow":
                continue
            entries.append({
                "name": str(item.relative_to(target)).replace("\\", "/"),
                "type": "directory" if item.is_dir() else "file",
                # 让后续工具可以直接复用经过策略检查的绝对路径，
                # 尤其是 vision_describe_image 观察目录中的图片。
                "path": str(item.resolve()),
            })
    except OSError as exc:
        return _result("list", "error", path, reason=str(exc))
    return _result("list", "success", path, entries=entries, truncated=truncated)


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=False, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(target)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise


@tool
def write_file(path: str, content: str, authorization_id: str | None = None) -> str:
    """写入文本文件；白名单外路径只返回 confirmation_required。"""
    target, error = _checked_path(path, "write", authorization_id)
    if error:
        return error
    try:
        size = len(content.encode("utf-8"))
        limit = max_bytes("write")
        if size > limit:
            return _result("write", "error", path, reason=f"内容超过 {limit} 字节限制")
        _atomic_write(target, content)
    except (OSError, TypeError) as exc:
        return _result("write", "error", path, reason=str(exc))
    finally:
        if authorization_id:
            consume_authorization(authorization_id)
    return _result("write", "success", path, bytes=size)


@tool
def apply_patch(path: str, old_content: str, new_content: str, authorization_id: str | None = None) -> str:
    """唯一匹配 old_content 后替换；找不到或匹配多次时不会写入。"""
    target, error = _checked_path(path, "patch", authorization_id)
    if error:
        return error
    if not isinstance(old_content, str) or not isinstance(new_content, str) or not old_content:
        return _result("patch", "error", path, reason="old_content 和 new_content 必须是非空字符串")
    try:
        if not target.is_file():
            return _result("patch", "error", path, reason="目标不是文件")
        limit = max_bytes("patch")
        if target.stat().st_size > limit:
            return _result("patch", "error", path, reason=f"文件超过 {limit} 字节限制")
        current = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _result("patch", "error", path, reason="只支持 UTF-8 文本文件")
    except OSError as exc:
        return _result("patch", "error", path, reason=str(exc))
    count = current.count(old_content)
    if count == 0:
        return _result("patch", "error", path, reason="目标内容未找到")
    if count > 1:
        return _result("patch", "error", path, reason="目标内容出现多次，无法确定替换位置")
    updated = current.replace(old_content, new_content, 1)
    try:
        size = len(updated.encode("utf-8"))
        if size > max_bytes("patch"):
            return _result("patch", "error", path, reason="补丁结果超过写入大小限制")
        _atomic_write(target, updated)
    except (OSError, TypeError) as exc:
        return _result("patch", "error", path, reason=str(exc))
    finally:
        if authorization_id:
            consume_authorization(authorization_id)
    return _result("patch", "success", path, bytes=size)


__all__ = ["read_file", "list_directory", "write_file", "apply_patch"]
