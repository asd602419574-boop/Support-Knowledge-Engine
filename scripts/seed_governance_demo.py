from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def main() -> None:
    from support_knowledge_engine.demo_data import seed_demo_data

    parser = argparse.ArgumentParser(description="导入虚构 PDF 并建立产品别名演示数据")
    parser.add_argument(
        "--database",
        default=str(PROJECT_ROOT / "instance" / "knowledge.db"),
        help="SQLite 数据库路径",
    )
    parser.add_argument(
        "--sample-directory",
        default=str(PROJECT_ROOT / "sample_docs"),
        help="虚构 PDF 目录",
    )
    args = parser.parse_args()
    result = seed_demo_data(args.database, args.sample_directory)
    print(
        f"演示数据就绪：产品 {result['products']}，别名 {result['aliases']}，"
        f"本次关联文档 {result['linked_documents']}。"
    )


if __name__ == "__main__":
    main()
