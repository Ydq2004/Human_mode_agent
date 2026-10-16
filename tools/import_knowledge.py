"""手动导入本地 PDF 的命令行入口：python -m tools.import_knowledge path.pdf"""

from __future__ import annotations

import argparse
import json

from memory.knowledge_base import ingest_pdf


def main() -> int:
    parser = argparse.ArgumentParser(description="导入 PDF 到本地知识库")
    parser.add_argument("path", help="PDF 文件路径")
    parser.add_argument("--force", action="store_true", help="即使内容未变化也重新建立索引")
    args = parser.parse_args()
    print(json.dumps(ingest_pdf(args.path, force=args.force), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
