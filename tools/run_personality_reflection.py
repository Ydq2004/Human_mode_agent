"""外部人格反思入口。

脚本每次只运行一轮检查，适合交给 Windows 任务计划程序。它不会把反思
塞进前台聊天循环；没有达到触发条件时，运行器会直接返回 skipped。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_openai import ChatOpenAI

from config import (
    APPRAISAL_LLM_MAX_TOKENS,
    APPRAISAL_LLM_TEMPERATURE,
    APPRAISAL_LLM_TIMEOUT_SECONDS,
    DEFAULT_THREAD_ID,
    LLM_MAX_RETRIES,
)
from memory.personality_reflection_runner import run_personality_reflection
from memory.personality_store import ensure_personality_seed_from_persona
from persona.loader import get_model_config, load_persona


def _build_reflection_llm():
    persona = load_persona()
    model_cfg = get_model_config(persona)
    key_name = str(model_cfg.get("api_key_env", "DEEPSEEK_API_KEY")).strip()
    api_key = os.environ.get(key_name, "").strip()
    if not api_key:
        raise RuntimeError(f"环境变量 {key_name} 未设置或为空")
    return ChatOpenAI(
        model=model_cfg.get("model_name", "deepseek-v4-flash"),
        api_key=api_key,
        base_url=model_cfg.get("base_url", "https://api.deepseek.com"),
        temperature=APPRAISAL_LLM_TEMPERATURE,
        max_tokens=APPRAISAL_LLM_MAX_TOKENS,
        timeout=APPRAISAL_LLM_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="运行一轮人格反思检查")
    parser.add_argument("--thread-id", default=DEFAULT_THREAD_ID)
    parser.add_argument("--persona-instance-id", default=None)
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="人工补测：忽略该线程已有反思水位，重新分析历史经历",
    )
    parser.add_argument("--min-new-slices", type=int, default=None)
    parser.add_argument("--min-interval-days", type=float, default=None)
    args = parser.parse_args()

    try:
        persona = load_persona()
        seed = ensure_personality_seed_from_persona(persona)
        instance_id = str(args.persona_instance_id or seed["persona_instance_id"]).strip()
        result = run_personality_reflection(
            persona_instance_id=instance_id,
            thread_id=args.thread_id,
            llm=_build_reflection_llm(),
            reprocess=args.reprocess,
            **({"min_new_slices": args.min_new_slices} if args.min_new_slices is not None else {}),
            **({"min_interval_days": args.min_interval_days} if args.min_interval_days is not None else {}),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"completed", "skipped"} else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
