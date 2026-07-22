from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .db import connect_database, init_database
from .repository import search_documents, search_with_context


def load_cases(cases_path: str | Path) -> list[dict]:
    with Path(cases_path).open("r", encoding="utf-8") as source:
        cases = json.load(source)
    if not isinstance(cases, list) or not cases:
        raise ValueError("评测集必须是非空 JSON 数组。")
    return cases


def _legacy_case(connection, case: dict) -> dict:
    results = search_documents(connection, case["question"])
    target_rank = None
    hit_document = False
    for rank, result in enumerate(results, start=1):
        if result["filename"] == case["target_document"]:
            hit_document = True
            if int(result["page_number"]) == int(case["target_page"]):
                target_rank = rank
                break
    forbidden = set(case.get("forbidden_products", []))
    found = sorted({str(row["canonical_product_name"]) for row in results[:5]
                    if row.get("canonical_product_name") in forbidden})
    passed = target_rank is not None and target_rank <= int(case["expected_max_rank"]) and not found
    return {
        "id": case["id"], "question": case["question"],
        "target_document": case["target_document"], "target_page": int(case["target_page"]),
        "hit_target_document": hit_document, "target_rank": target_rank,
        "forbidden_product_found": bool(found), "forbidden_products_found": found,
        "match_state": "legacy_baseline", "should_return_answer": True,
        "returned_answer": bool(results), "alias_recognized": None,
        "outdated_mis_hit": False, "passed": passed, "notes": case.get("notes", ""),
        "elapsed_ms": 0.0,
    }


def evaluate_case(connection, case: dict) -> dict:
    if "should_return_answer" not in case:
        return _legacy_case(connection, case)
    outcome = search_with_context(connection, case["question"])
    results = outcome["results"]
    target_document = case.get("target_document")
    target_page = case.get("target_page")
    target_rank = None
    hit_document = False
    for rank, result in enumerate(results, start=1):
        if target_document and result["filename"] == target_document:
            hit_document = True
            if target_page is None or int(result["page_number"]) == int(target_page):
                target_rank = rank
                break
    forbidden = set(case.get("forbidden_products", []))
    found = sorted({str(row["canonical_product_name"]) for row in results[:3]
                    if row.get("canonical_product_name") in forbidden})
    allowed_statuses = set(case.get("allowed_document_statuses", []))
    outdated_mis_hit = bool(allowed_statuses) and any(
        str(row["status"]) not in allowed_statuses for row in results[:3]
    )
    should_answer = bool(case["should_return_answer"])
    returned_answer = bool(results) and outcome["match_state"] != "insufficient_evidence"
    if should_answer:
        passed = target_rank is not None and target_rank <= int(case.get("expected_max_rank", 3))
        passed = passed and not found and not outdated_mis_hit
    else:
        passed = not returned_answer
    expected_product = case.get("target_product")
    recognized = {item["name"] for item in outcome["recognized_products"]}
    alias_expected = bool(case.get("alias_expected", False))
    alias_recognized = (expected_product in recognized) if alias_expected else None
    return {
        "id": case["id"], "category": case.get("category", "uncategorized"),
        "question": case["question"], "target_product": expected_product,
        "allowed_products": case.get("allowed_products", []),
        "target_document": target_document, "target_page": target_page,
        "hit_target_document": hit_document, "target_rank": target_rank,
        "forbidden_product_found": bool(found), "forbidden_products_found": found,
        "match_state": outcome["match_state"], "should_return_answer": should_answer,
        "returned_answer": returned_answer, "alias_recognized": alias_recognized,
        "outdated_mis_hit": outdated_mis_hit, "passed": passed,
        "notes": case.get("notes", ""), "elapsed_ms": outcome["elapsed_ms"],
    }


def _metrics(results: list[dict]) -> dict:
    answer_cases = [row for row in results if row["should_return_answer"]]
    no_answer = [row for row in results if not row["should_return_answer"]]
    alias_cases = [row for row in results if row["alias_recognized"] is not None]
    outdated_cases = [row for row in results if row.get("category") != "outdated_only"]
    reciprocal = [1 / row["target_rank"] if row["target_rank"] else 0 for row in answer_cases]
    return {
        "recall_at_1": sum(row["target_rank"] == 1 for row in answer_cases) / max(1, len(answer_cases)),
        "recall_at_3": sum(row["target_rank"] is not None and row["target_rank"] <= 3 for row in answer_cases) / max(1, len(answer_cases)),
        "mrr": sum(reciprocal) / max(1, len(reciprocal)),
        "product_leakage_rate": sum(row["forbidden_product_found"] for row in results) / max(1, len(results)),
        "outdated_document_mis_hit_rate": sum(row["outdated_mis_hit"] for row in outdated_cases) / max(1, len(outdated_cases)),
        "no_answer_false_return_rate": sum(row["returned_answer"] for row in no_answer) / max(1, len(no_answer)),
        "alias_recognition_success_rate": sum(bool(row["alias_recognized"]) for row in alias_cases) / max(1, len(alias_cases)),
        "average_search_ms": sum(row["elapsed_ms"] for row in results) / max(1, len(results)),
    }


def _markdown(report: dict) -> str:
    metrics = report["metrics"]
    lines = [
        "# Support Knowledge Engine 第三阶段检索评测", "",
        f"- 生成时间：{report['generated_at']}", f"- 用例数：{report['total']}",
        f"- 通过数：{report['passed']}", f"- 通过率：{report['pass_rate']:.1%}",
        f"- Recall@1：{metrics['recall_at_1']:.3f}", f"- Recall@3：{metrics['recall_at_3']:.3f}",
        f"- MRR：{metrics['mrr']:.3f}", f"- 产品串库率：{metrics['product_leakage_rate']:.3f}",
        f"- 过期文档误命中率：{metrics['outdated_document_mis_hit_rate']:.3f}",
        f"- 无答案错误返回率：{metrics['no_answer_false_return_rate']:.3f}",
        f"- 别名识别成功率：{metrics['alias_recognition_success_rate']:.3f}",
        f"- 平均检索耗时：{metrics['average_search_ms']:.3f} ms", "",
        "## 失败案例", "",
    ]
    failures = [row for row in report["cases"] if not row["passed"]]
    if failures:
        for row in failures:
            reasons = []
            if row["target_rank"] is None and row["should_return_answer"]:
                reasons.append("目标页未命中")
            if row["forbidden_product_found"]:
                reasons.append("混入禁止产品")
            if row["outdated_mis_hit"]:
                reasons.append("文档状态不符合预期")
            if not row["should_return_answer"] and row["returned_answer"]:
                reasons.append("无答案问题错误返回")
            lines.append(f"- **{row['id']}** `{row['question']}`：{'；'.join(reasons) or '排名超出预期'}")
    else:
        lines.append("- 无")
    lines.extend(["", "## 全部案例", "", "| 用例 | 问题 | 分类 | 目标排名 | 状态 | 结果 |", "|---|---|---|---:|---|---|"])
    for row in report["cases"]:
        question = str(row["question"]).replace("|", "\\|")
        lines.append(f"| {row['id']} | {question} | {row.get('category', '')} | {row['target_rank'] or '—'} | {row['match_state']} | {'通过' if row['passed'] else '失败'} |")
    return "\n".join(lines) + "\n"


def run_evaluation(database_path: str | Path, cases_path: str | Path, output_path: str | Path) -> dict:
    init_database(database_path)
    cases = load_cases(cases_path)
    with connect_database(database_path) as connection:
        results = [evaluate_case(connection, case) for case in cases]
    passed = sum(row["passed"] for row in results)
    report = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "dataset": Path(cases_path).name, "total": len(results), "passed": passed,
        "pass_rate": passed / len(results), "metrics": _metrics(results), "cases": results,
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown_path = output if output.suffix.lower() != ".json" else output.with_suffix(".md")
    json_path = output if output.suffix.lower() == ".json" else output.with_suffix(".json")
    markdown_path.write_text(_markdown(report), encoding="utf-8", newline="\n")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="运行可重复的检索质量评测")
    parser.add_argument("--database", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--output", required=True, help="Markdown 或 JSON 路径；同时生成另一格式")
    args = parser.parse_args()
    report = run_evaluation(args.database, args.cases, args.output)
    print(json.dumps({"passed": report["passed"], "total": report["total"], **report["metrics"]}, ensure_ascii=False))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
