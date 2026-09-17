"""安全数值计算工具；不执行任意 Python 代码。"""

from __future__ import annotations

import ast
import math
import operator
from langchain_core.tools import tool
from tools.tool_contracts import tool_result

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max}


def _eval(node):
    if isinstance(node, ast.Expression): return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool): return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN: return _BIN[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY: return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(arg) for arg in node.args])
    raise ValueError("表达式包含不允许的语法")


@tool
def calculate(expression: str) -> str:
    """计算受限数学表达式，不访问文件、网络或系统命令。"""
    expression = str(expression or "").strip()
    if not expression or len(expression) > 500:
        return tool_result("calculate", "invalid_input", error="表达式为空或过长")
    try:
        value = _eval(ast.parse(expression, mode="eval"))
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("结果不是有限数字")
    except Exception as exc:
        return tool_result("calculate", "failed", error=str(exc))
    return tool_result("calculate", "success", result={"expression": expression, "value": value})


__all__ = ["calculate"]
