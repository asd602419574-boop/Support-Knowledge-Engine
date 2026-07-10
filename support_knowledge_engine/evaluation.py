from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import connect_database, init_database
from .repository import search_documents


@dataclass(frozen=True)
class CaseResult:
    id: str
    question: str
    target_document: str
    target_page: int
    hit_target_document: bool
    target_rank: int | None
    forbidden_product_found: bool
    forbidden_products_found: list[str]
    passed: bool
    notes: str


def load_cases(cases_path: str | Path) -> list[dict]:
    with Path(cases_path).open("r", encoding="utf-8") as source:
        cases = json.load(source)
    if not isinstance(cases, list) or not cases:
        raise ValueError("评测集必须是非空 JSON 数组。")
    return cases


def evaluate_case(connection, case: dict, forbidden_top_k: int = 5) -> CaseResult:
    results = search_documents(connection, case["question"])
    target_rank: int | None = None
    hit_target_document = False
    for rank, result in enumerate(results, start=1):
        if result["filename"] == case["target_document"]:
            hit_target_document = True
            if int(result["page_number"]) == int(case["target_page"]):
                target_rank = rank
                break

    forbidden = set(case.get("forbidden_products", []))
    forbidden_found = sorted(
        {
            str(result["canonical_product_name"])
            for result in results[:forbidden_top_k]
            if result.get("canonical_product_name") in forbidden
        }
    )
    passed = (
        target_rank is not None
        and target_rank <= int(case["expected_max_rank"])
        and not forbidden_found
    )
    return CaseResult(
        id=case["id"],
        question=case["question"],
        target_document=case["target_document"],
        target_page=int(case["target_page"]),
        hit_target_document=hit_target_document,
        target_rank=target_rank,
        forbidden_product_found=bool(forbidden_found),
        forbidden_products_found=forbidden_found,
        passed=passed,
        notes=case.get("notes", ""),
    )


def _markdown_report(report: dict) -> str:
    lines = [
        "# Support Knowledge Engine 检索评测报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 用例数：{report['total']}",
        f"- 通过数：{report['passed']}",
        f"- 通过率：{report['pass_rate']:.1%}",
        "",
        "| 用例 | 问题 | 命中文档 | 目标排名 | 禁止产品混入 | 结果 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for result in report["cases"]:
        lines.append(
            "| {id} | {question} | {hit} | {rank} | {forbidden} | {passed} |".format(
                id=result["id"],
                question=result["question"].replace("|", "\\|"),
                hit="是" if result["hit_target_document"] else "否",
                rank=result["target_rank"] or "—",
                forbidden="是" if result["forbidden_product_found"] else "否",
                passed="通过" if result["passed"] else "失败",
            )
        )
    lines.extend(["", "## 用例说明", ""])
    for result in report["cases"]:
        lines.append(
            f"- **{result['id']}**：目标 `{result['target_document']}` 第 {result['target_page']} 页。{result['notes']}"
        )
    return "\n".join(lines) + "\n"


def run_evaluation(
    database_path: str | Path,
    cases_path: str | Path,
    output_path: str | Path,
) -> dict:
    init_database(database_path)
    cases = load_cases(cases_path)
    with connect_database(database_path) as connection:
        results = [evaluate_case(connection, case) for case in cases]
    passed = sum(result.passed for result in results)
    report = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "total": len(results),
        "passed": passed,
        "pass_rate": passed / len(results),
        "cases": [asdict(result) for result in results],
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".json":
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    else:
        output.write_text(_markdown_report(report), encoding="utf-8", newline="\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 Support Knowledge Engine 检索质量评测")
    parser.add_argument("--database", required=True, help="SQLite 数据库路径")
    parser.add_argument("--cases", required=True, help="评测 JSON 路径")
    parser.add_argument("--output", required=True, help="Markdown 或 JSON 报告路径")
    args = parser.parse_args()
    report = run_evaluation(args.database, args.cases, args.output)
    print(
        f"评测完成：{report['passed']}/{report['total']} 通过，"
        f"通过率 {report['pass_rate']:.1%}，报告：{args.output}"
    )
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
