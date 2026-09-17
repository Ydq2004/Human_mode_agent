"""通用工具结果协议。工具返回真实执行状态，不向 Agent 伪造副作用。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def tool_result(tool: str, status: str, *, result: Any = None, error: str | None = None, **extra: Any) -> str:
    payload = {
        "tool": str(tool),
        "operation_id": str(extra.pop("operation_id", "")),
        "status": str(status),
        "result": result,
        "error": error,
        "source": str(extra.pop("source", "local")),
        "started_at": extra.pop("started_at", datetime.now(timezone.utc).isoformat()),
        "completed_at": extra.pop("completed_at", datetime.now(timezone.utc).isoformat()),
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, default=str)


def parse_tool_result(content: str) -> dict[str, Any] | None:
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
