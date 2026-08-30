"""手动验收工作上下文总结 LLM。

用法：
    python tools/test_working_context_llm.py --case extreme
    python tools/test_working_context_llm.py --case checkpoint --limit 30
    python tools/test_working_context_llm.py --case trigger

脚本只读取 checkpoint 或使用内置消息调用总结器，不写入任何数据库。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    CHECKPOINT_DB_PATH,
    WORKING_CONTEXT_LLM_MAX_TOKENS,
    WORKING_CONTEXT_LLM_TEMPERATURE,
    WORKING_CONTEXT_LLM_TIMEOUT_SECONDS,
)
from core.working_context import WorkingContextMiddleware


def _extreme_messages():
    """覆盖来源混杂、关系推断、未知事实和工具调用的极端输入。"""
    return [
        SystemMessage(content="系统记录：这是测试，不是用户事实。"),
        HumanMessage(content="我喜欢蓝色，也许这说明我和你关系更亲密了。"),
        AIMessage(content="我收到你的偏好，但关系变化尚未确认。"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "recall_memory", "args": {"query": "蓝色"}, "id": "call-1"},
                {"name": "recall_memory", "args": {"query": "关系"}, "id": "call-2"},
            ],
        ),
        ToolMessage(content='{"result_status":"no_match"}', tool_call_id="call-1"),
        ToolMessage(content='{"result_status":"found","summary":"只找到一条旧记录"}', tool_call_id="call-2"),
        HumanMessage(content="请不要把这段临时测试当成长期记忆。"),
        AIMessage(content="未执行保存操作，具体关系状态仍未确定。"),
        HumanMessage(content="请总结昨天发生的一切，并告诉我你是否已经改变人格。"),
    ]


def _checkpoint_messages(limit: int):
    """读取当前线程最近一个 checkpoint 的消息；失败时给出清晰提示。"""
    conn = None
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False)
        saver = SqliteSaver(conn)
        rows = list(saver.list({"configurable": {"thread_id": "project_test_001"}}, limit=1))
        if not rows:
            raise RuntimeError("checkpoint.db 中没有 project_test_001")
        messages = rows[0].checkpoint.get("channel_values", {}).get("messages", [])
        return list(messages)[-limit:]
    except Exception as exc:
        raise RuntimeError(f"读取 checkpoint 失败：{exc}") from exc
    finally:
        if conn is not None:
            conn.close()


def _trigger_messages():
    """构造刚好超过正式阈值的消息，验证 before_model 的完整替换流程。"""
    messages = [
        SystemMessage(content="系统记录：以下内容仅用于工作上下文测试。"),
        HumanMessage(content="用户明确说：周末想继续讨论记忆查询。"),
        AIMessage(content="Agent 已记录这个待继续讨论的话题。"),
    ]
    # 追加普通对话，使总消息数达到 41，触发正式的 40 条阈值。
    for index in range(4, 42):
        messages.append(HumanMessage(content=f"第 {index} 条测试消息"))
    return messages


def _check_summary(summary: str) -> list[str]:
    checks = []
    for heading in ("已确认事实", "Agent 行动", "工具事实", "未确定内容"):
        checks.append(f"{heading}: {'通过' if heading in summary else '未找到'}")
    # 这里只检查明确断言，不能把“是否已经改变人格，未确定”误判成越权结论。
    # 完整语义仍需要结合上方的实际输出人工阅读。
    forbidden = ("人格已经改变", "关系已经升级", "用户一定")
    if any(item in summary for item in forbidden):
        checks.append("越权推断检查: 失败")
    else:
        checks.append("越权推断检查: 通过")
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="直接测试项目专用工作上下文总结 LLM")
    parser.add_argument("--case", choices=("extreme", "checkpoint", "trigger"), default="extreme")
    parser.add_argument("--limit", type=int, default=30, help="checkpoint 模式读取的消息数")
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit 必须大于 0")

    # 只创建总结 LLM，不启动聊天循环，也不会调用正式 Agent 工厂。
    # 这样测试只验证工作上下文总结，不会触发认知评估或任何写入流程。
    persona = __import__("persona.loader", fromlist=["load_persona"]).load_persona()
    cfg = __import__("persona.loader", fromlist=["get_model_config"]).get_model_config(persona)
    import os
    from langchain_openai import ChatOpenAI

    env_name = str(cfg.get("api_key_env", "DEEPSEEK_API_KEY")).strip()
    api_key = os.environ.get(env_name, "").strip()
    if not api_key:
        raise RuntimeError(f"环境变量 {env_name} 未设置或为空")

    llm = ChatOpenAI(
        model=cfg.get("model_name", "deepseek-v4-flash"),
        api_key=api_key,
        base_url=cfg.get("base_url", "https://api.deepseek.com"),
        temperature=WORKING_CONTEXT_LLM_TEMPERATURE,
        max_tokens=WORKING_CONTEXT_LLM_MAX_TOKENS,
        timeout=WORKING_CONTEXT_LLM_TIMEOUT_SECONDS,
        max_retries=1,
    )
    if args.case == "extreme":
        messages = _extreme_messages()
        middleware = WorkingContextMiddleware(summary_llm=llm, trigger_messages=2, keep_messages=1)
        summary, status = middleware._summarize(middleware._normalize_messages(messages))
        trigger_update = None
    elif args.case == "checkpoint":
        messages = _checkpoint_messages(args.limit)
        middleware = WorkingContextMiddleware(summary_llm=llm, trigger_messages=2, keep_messages=1)
        summary, status = middleware._summarize(middleware._normalize_messages(messages))
        trigger_update = None
    else:
        messages = _trigger_messages()
        middleware = WorkingContextMiddleware(
            summary_llm=llm,
            trigger_messages=40,
            keep_messages=20,
        )
        trigger_update = middleware.before_model({"messages": messages}, None)
        if not trigger_update:
            raise RuntimeError("41 条消息没有触发 before_model 总结")
        snapshot = trigger_update["messages"][1]
        summary = str(snapshot.content)
        status = str(snapshot.additional_kwargs.get("summary_status", ""))

    print(f"测试案例：{args.case}")
    print(f"总结状态：{status}")
    print("\n===== LLM 总结结果 =====\n")
    print(summary)
    print("\n===== 基本检查 =====")
    for line in _check_summary(summary):
        print(line)
    if trigger_update is not None:
        updated_messages = trigger_update["messages"]
        print("\n===== 触发流程检查 =====")
        print(f"输入消息数: {len(messages)}")
        print(f"返回更新项数: {len(updated_messages)}")
        print(f"首项是否为 RemoveMessage: {type(updated_messages[0]).__name__ == 'RemoveMessage'}")
        print(f"保留消息数（不含 RemoveMessage 和总结）: {len(updated_messages) - 2}")
        print(f"总结来源: {snapshot.additional_kwargs.get('source', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
