"""查看或人工收口跨进程提交账本中的未完成任务。

默认只读，不会重跑任务，也不会修改数据库：

    python tools/recover_commit_ledger.py
    python tools/recover_commit_ledger.py --thread-id project_test_001

只有明确传入 ``--abandon`` 时，才会把指定任务标记为 abandoned。这个动作
不会删除账本记录，方便以后审计为什么没有自动恢复。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import DEFAULT_THREAD_ID
from memory import commit_store


def main() -> int:
    parser = argparse.ArgumentParser(description="查看或收口提交账本任务")
    parser.add_argument("--thread-id", default=DEFAULT_THREAD_ID)
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24.0,
        help="只显示最近多少小时内的已完成评价等待提交任务；用 -1 关闭时间筛选",
    )
    parser.add_argument(
        "--abandon",
        nargs="+",
        metavar="JOB_ID",
        help="明确把指定任务标记为 abandoned，不会删除记录",
    )
    parser.add_argument(
        "--reason",
        default="人工确认后放弃自动恢复",
        help="标记 abandoned 时保存的原因",
    )
    args = parser.parse_args()

    if args.max_age_hours < 0:
        max_age_hours = None
    else:
        max_age_hours = args.max_age_hours

    if args.abandon:
        changed = []
        for job_id in args.abandon:
            if commit_store.mark_commit_abandoned(job_id, args.reason):
                changed.append(job_id)
        print(json.dumps({"abandoned": changed}, ensure_ascii=False, indent=2))

    rows = commit_store.list_recoverable_commits(
        thread_id=args.thread_id,
        max_age_hours=max_age_hours,
    )
    unfinished = commit_store.list_unfinished_commits()
    print("\n可恢复候选（评价已完成、提交等待中）：")
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    print("\n全部未完成账本任务：")
    print(json.dumps(unfinished, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
