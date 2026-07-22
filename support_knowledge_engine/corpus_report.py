from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .db import connect_database, init_database


def _duration_seconds(start: str, finish: str) -> float:
    return max(0.0, (datetime.fromisoformat(finish) - datetime.fromisoformat(start)).total_seconds())


def collect_corpus_statistics(database_path: str | Path) -> dict:
    init_database(database_path)
    with connect_database(database_path) as connection:
        scalar = lambda sql: connection.execute(sql).fetchone()[0]
        type_rows = connection.execute(
            "SELECT document_type, COUNT(*) AS count FROM documents GROUP BY document_type ORDER BY count DESC"
        ).fetchall()
        status_rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM documents GROUP BY status ORDER BY status"
        ).fetchall()
        missing_rows = connection.execute(
            """SELECT field_name, COUNT(*) AS count FROM document_field_values
               WHERE COALESCE(revised_value, extracted_value) = '待确认'
               GROUP BY field_name ORDER BY field_name"""
        ).fetchall()
        runs = connection.execute(
            "SELECT started_at, finished_at, imported_count, duration_ms FROM import_runs WHERE finished_at IS NOT NULL"
        ).fetchall()
        durations = [
            ((row["duration_ms"] / 1000) if row["duration_ms"] is not None
             else _duration_seconds(row["started_at"], row["finished_at"])) / max(1, row["imported_count"])
            for row in runs if row["imported_count"]
        ]
        return {
            "document_count": scalar("SELECT COUNT(*) FROM documents"),
            "page_count": scalar("SELECT COUNT(*) FROM pages"),
            "fts_entry_count": scalar("SELECT COUNT(*) FROM page_fts"),
            "product_count": scalar("SELECT COUNT(*) FROM products"),
            "document_type_distribution": {row["document_type"]: row["count"] for row in type_rows},
            "status_distribution": {row["status"]: row["count"] for row in status_rows},
            "missing_field_count": sum(row["count"] for row in missing_rows),
            "missing_fields": {row["field_name"]: row["count"] for row in missing_rows},
            "duplicate_file_count": scalar("SELECT COUNT(*) FROM import_items WHERE outcome = '重复'"),
            "parse_failure_count": scalar("SELECT COUNT(*) FROM import_items WHERE outcome = '失败'"),
            "average_import_seconds": sum(durations) / len(durations) if durations else 0.0,
            "database_size_bytes": Path(database_path).stat().st_size,
        }


def write_corpus_report(database_path: str | Path, markdown_path: str | Path,
                        json_path: str | Path | None = None) -> dict:
    stats = collect_corpus_statistics(database_path)
    markdown = Path(markdown_path)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 第三阶段语料统计报告", "",
        f"- 文档数量：{stats['document_count']}", f"- 页数：{stats['page_count']}",
        f"- FTS 条目数：{stats['fts_entry_count']}", f"- 产品数量：{stats['product_count']}",
        f"- 缺失字段数量：{stats['missing_field_count']}", f"- 重复文件数量：{stats['duplicate_file_count']}",
        f"- 解析失败数量：{stats['parse_failure_count']}",
        f"- 平均导入时间：{stats['average_import_seconds']:.4f} 秒/文档",
        f"- 数据库大小：{stats['database_size_bytes']} 字节", "", "## 文档类型分布", "",
    ]
    lines.extend(f"- {key}：{value}" for key, value in stats["document_type_distribution"].items())
    lines.extend(["", "## 状态分布", ""])
    lines.extend(f"- {key}：{value}" for key, value in stats["status_distribution"].items())
    lines.extend(["", "## 缺失字段", ""])
    lines.extend(f"- {key}：{value}" for key, value in stats["missing_fields"].items())
    markdown.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    if json_path:
        target = Path(json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="生成语料统计 Markdown/JSON 报告")
    parser.add_argument("--database", required=True)
    parser.add_argument("--markdown", required=True)
    parser.add_argument("--json")
    args = parser.parse_args()
    stats = write_corpus_report(args.database, args.markdown, args.json)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
