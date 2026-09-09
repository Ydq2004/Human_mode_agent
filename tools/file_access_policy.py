"""文件工具的实例级权限策略与路径安全检查。

这里不执行文件读写，只回答“这个操作是否允许”。实际副作用由
``file_tools`` 执行。策略文件可以随实例调整，但系统目录和敏感文件
始终由框架规则拒绝，不能靠模型参数绕过。
"""

from __future__ import annotations

import fnmatch
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import FILE_ACCESS_POLICY_PATH, PROJECT_ROOT


@dataclass(frozen=True)
class PathCheckResult:
    status: str  # allow / confirmation_required / denied
    operation: str
    requested_path: str
    resolved_path: str = ""
    reason: str = ""
    operation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": self.operation,
            "requested_path": self.requested_path,
            "resolved_path": self.resolved_path,
            "reason": self.reason,
            "operation_id": self.operation_id,
        }


_AUTHORIZATIONS: dict[str, tuple[str, str]] = {}
_PENDING_OPERATIONS: dict[str, tuple[str, str]] = {}


def _normalized(path: Path) -> str:
    value = os.path.normpath(str(path))
    return os.path.normcase(value).replace("\\", "/").rstrip("/")


def _within(path: Path, root: Path) -> bool:
    candidate = _normalized(path)
    allowed = _normalized(root)
    return candidate == allowed or candidate.startswith(allowed + "/")


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    policy_path = Path(path or FILE_ACCESS_POLICY_PATH)
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("文件权限配置必须是 JSON object")

    def roots(name: str) -> list[Path]:
        values = raw.get(name, [])
        if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
            raise ValueError(f"{name} 必须是非空字符串列表")
        result = []
        for value in values:
            root = Path(value)
            if not root.is_absolute():
                # 保持现有策略兼容：相对白名单以项目根目录解释。
                # 未来若需要槽位内专属目录，应在策略中显式声明路径基准，
                # 不能因为切换实例而悄悄改变旧配置的含义。
                root = PROJECT_ROOT / root
            result.append(root.resolve(strict=False))
        return result

    sensitive = raw.get("sensitive_patterns", [])
    if not isinstance(sensitive, list) or not all(isinstance(v, str) and v.strip() for v in sensitive):
        raise ValueError("sensitive_patterns 必须是字符串列表")
    max_read = raw.get("max_read_bytes", 0)
    max_write = raw.get("max_write_bytes", 0)
    if isinstance(max_read, bool) or not isinstance(max_read, int) or max_read <= 0:
        raise ValueError("max_read_bytes 必须是正整数")
    if isinstance(max_write, bool) or not isinstance(max_write, int) or max_write <= 0:
        raise ValueError("max_write_bytes 必须是正整数")
    return {
        "read_roots": roots("read_roots"),
        "write_roots": roots("write_roots"),
        "sensitive_patterns": [v.lower().strip() for v in sensitive],
        "max_read_bytes": max_read,
        "max_write_bytes": max_write,
    }


def _is_sensitive(path: Path, patterns: list[str]) -> bool:
    names = [_normalized(path), path.name.lower()]
    parts = [part.lower() for part in path.parts]
    names.extend(parts)
    return any(fnmatch.fnmatch(value, pattern) for value in names for pattern in patterns)


def _is_system_path(path: Path) -> bool:
    resolved = _normalized(path)
    system_roots = []
    if os.name == "nt":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        system_roots.extend([Path(windir), Path(windir) / "System32"])
        program_data = os.environ.get("PROGRAMDATA")
        if program_data:
            system_roots.append(Path(program_data))
    else:
        system_roots.extend([Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin"), Path("/root")])
    return any(_within(Path(resolved), root.resolve(strict=False)) for root in system_roots)


def check_path(path: str, operation: str, *, policy_path: str | Path | None = None,
               authorization_id: str | None = None) -> PathCheckResult:
    if operation not in {"read", "list", "write", "patch"}:
        raise ValueError("operation 必须是 read/list/write/patch")
    requested = str(path or "").strip()
    if not requested:
        return PathCheckResult("denied", operation, requested, reason="路径不能为空")
    policy = load_policy(policy_path)
    original = Path(requested)
    if not original.is_absolute():
        original = PROJECT_ROOT / original
    resolved = original.resolve(strict=False)
    if _is_system_path(resolved):
        return PathCheckResult("denied", operation, requested, str(resolved), "系统目录禁止访问")
    if _is_sensitive(resolved, policy["sensitive_patterns"]):
        return PathCheckResult("denied", operation, requested, str(resolved), "敏感文件禁止访问")

    roots = policy["read_roots"] if operation in {"read", "list"} else policy["write_roots"]
    if any(_within(resolved, root) for root in roots):
        return PathCheckResult("allow", operation, requested, str(resolved))

    operation_id = f"fileop_{secrets.token_urlsafe(12)}"
    if authorization_id and _AUTHORIZATIONS.get(authorization_id) == (operation, _normalized(resolved)):
        return PathCheckResult("allow", operation, requested, str(resolved), "用户授权通过", operation_id)
    _PENDING_OPERATIONS[operation_id] = (operation, _normalized(resolved))
    return PathCheckResult(
        "confirmation_required" if operation in {"write", "patch"} else "denied",
        operation,
        requested,
        str(resolved),
        "路径不在白名单内，需要用户确认" if operation in {"write", "patch"} else "路径不在读取白名单内",
        operation_id,
    )


def grant_authorization(operation_id: str, path: str, operation: str, *, policy_path: str | Path | None = None) -> str:
    """由外层用户确认流程调用，模型本身不应调用此函数。"""
    check = check_path(path, operation, policy_path=policy_path)
    expected = _PENDING_OPERATIONS.get(str(operation_id))
    requested_key = _normalized(Path(check.resolved_path)) if check.resolved_path else ""
    if check.status != "confirmation_required" or expected != (operation, requested_key):
        raise ValueError("授权请求已失效，或操作与路径不匹配")
    token = f"auth_{secrets.token_urlsafe(18)}"
    _AUTHORIZATIONS[token] = (operation, _normalized(Path(check.resolved_path)))
    _PENDING_OPERATIONS.pop(str(operation_id), None)
    _PENDING_OPERATIONS.pop(check.operation_id, None)
    return token


def consume_authorization(authorization_id: str | None) -> None:
    """成功执行一次白名单外操作后使授权令牌失效。"""
    if authorization_id:
        _AUTHORIZATIONS.pop(authorization_id, None)


def max_bytes(operation: str, *, policy_path: str | Path | None = None) -> int:
    policy = load_policy(policy_path)
    return policy["max_read_bytes"] if operation in {"read", "list"} else policy["max_write_bytes"]
