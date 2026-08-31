"""外部定时任务入口：检查并生成周期摘要。

本脚本只运行一次，适合交给 Windows 任务计划程序或 cron 每天调用：
    python tools/run_periodic_summaries.py --thread-id project_test_001
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
    DEFAULT_THREAD_ID,
    LLM_MAX_RETRIES,
    SUMMARY_LLM_MAX_TOKENS,
    SUMMARY_LLM_TEMPERATURE,
    SUMMARY_LLM_TIMEOUT_SECONDS,
)
from memory.summary_scheduler import generate_missing_period_summaries
from persona.loader import get_model_config, load_persona


def _build_summary_llm():
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
        temperature=SUMMARY_LLM_TEMPERATURE,
        max_tokens=SUMMARY_LLM_MAX_TOKENS,
        timeout=SUMMARY_LLM_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="运行一次日/周/月情景摘要检查")
    parser.add_argument("--thread-id", default=DEFAULT_THREAD_ID)
    parser.add_argument("--no-llm", action="store_true", help="周/月使用代码摘要，不调用 LLM")
    args = parser.parse_args()
    try:
        llm = None if args.no_llm else _build_summary_llm()
        summaries = generate_missing_period_summaries(
            thread_id=args.thread_id,
            summary_llm=llm,
            # 定时任务需要让任务计划程序知道本次是否失败；
            # 调度器仍会先处理完所有周期，再统一报告失败。
            raise_on_failure=True,
        )
        print(json.dumps({
            "generated": [
                {
                    "summary_id": item.summary_id,
                    "summary_type": item.summary_type,
                    "source_slice_count": len(item.source_slice_ids),
                }
                for item in summaries
            ]
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
