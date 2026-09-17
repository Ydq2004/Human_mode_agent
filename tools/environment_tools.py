"""低风险环境查询工具。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from langchain_core.tools import tool
from tools.tool_contracts import tool_result


@tool
def get_current_time(timezone: str = "Asia/Shanghai") -> str:
    """读取机器当前时间；不能用于修改系统时间。"""
    try:
        now = datetime.now(ZoneInfo(str(timezone or "").strip()))
    except (ZoneInfoNotFoundError, ValueError):
        return tool_result("get_current_time", "invalid_input", error="无效时区")
    return tool_result("get_current_time", "success", result={"time": now.isoformat(), "timezone": timezone})


__all__ = ["get_current_time"]
