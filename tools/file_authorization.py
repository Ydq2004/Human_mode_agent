"""文件工具的用户确认中间件。

权限策略决定某个操作是否需要确认；本模块只负责把这个状态接到
Agent 的工具调用流程中。用户确认后，原工具调用会携带一次性令牌
继续执行，Agent 能在同一轮看到真实的工具结果。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from tools.file_access_policy import check_path, grant_authorization


_FILE_OPERATIONS = {
    "write_file": "write",
    "apply_patch": "patch",
}


def _confirmation_result(check) -> str:
    return json.dumps(
        {
            "operation": check.operation,
            "status": "confirmation_required",
            "path": check.requested_path,
            "reason": check.reason,
            "operation_id": check.operation_id,
        },
        ensure_ascii=False,
    )


def make_file_authorization_middleware(
    confirm: Callable[[Mapping[str, Any]], bool] | None,
):
    """创建文件授权中间件。

    ``confirm`` 由外层程序提供，返回 True 才会继续执行白名单外写入。
    传入 None 时不弹出交互，只返回 confirmation_required，适合服务端
    或测试环境；模型不能通过工具参数自行绕过这个回调。
    """

    @wrap_tool_call
    def authorize_file_call(request, handler):
        tool_call = request.tool_call or {}
        tool_name = str(tool_call.get("name") or "")
        operation = _FILE_OPERATIONS.get(tool_name)
        if operation is None:
            return handler(request)

        args = tool_call.get("args") or {}
        path = args.get("path") if isinstance(args, Mapping) else None
        check = check_path(str(path or ""), operation)
        if check.status != "confirmation_required":
            return handler(request)

        request_data = check.to_dict()
        request_data["tool_name"] = tool_name
        request_data["args"] = dict(args) if isinstance(args, Mapping) else {}
        if confirm is None or not bool(confirm(request_data)):
            return ToolMessage(
                content=_confirmation_result(check),
                tool_call_id=str(tool_call.get("id") or ""),
                name=tool_name,
            )

        token = grant_authorization(
            check.operation_id,
            check.requested_path,
            operation,
        )
        modified_call = {
            **tool_call,
            "args": {
                **dict(args),
                "authorization_id": token,
            },
        }
        return handler(request.override(tool_call=modified_call))

    return authorize_file_call


__all__ = ["make_file_authorization_middleware"]
