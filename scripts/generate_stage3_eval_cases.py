from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "evals" / "corpus_pilot_cases.json"

ALIASES = {
    "AeroCam Mini 2": ("ACM2", "航拍迷你二代"),
    "AeroCam Pro 2": ("ACP2", "航拍专业二代"),
    "AeroCam Mini 3": ("ACM3", "航拍迷你三代"),
    "AeroCam Pro 3": ("ACP3", "航拍专业三代"),
}

ACTIVE = [
    ("AeroCam Mini 2", "AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf", 3, "gimbal home sensor", "effective"),
    ("AeroCam Mini 2", "AeroCam-Mini-2_Release-Notes_v3.0_en-US.pdf", 2, "battery handshake AM2-31", "effective"),
    ("AeroCam Mini 2", "AeroCam-Mini-2_配件指南_v1.1_zh-CN.pdf", 2, "折叠支架锁扣", "effective"),
    ("AeroCam Mini 2", "AeroCam-Mini-2_快速入门_zh-CN.pdf", 2, "首次配对蓝灯", "needs_review"),
    ("AeroCam Pro 2", "AeroCam-Pro-2_Service-Handbook_v4.0_en-US.pdf", 2, "dual-camera thermal alignment", "effective"),
    ("AeroCam Pro 2", "AeroCam-Pro-2_Release-Notes_v4.0_en-US.pdf", 2, "payload firmware AP2-44", "effective"),
    ("AeroCam Pro 2", "AeroCam-Pro-2_配件指南_v2.0_zh-CN.pdf", 2, "双镜头保护框", "effective"),
    ("AeroCam Pro 2", "AeroCam-Pro-2_快速入门_zh-CN.pdf", 2, "载荷舱解锁", "needs_review"),
    ("AeroCam Mini 3", "AeroCam-Mini-3_Service-Handbook_v2.0_en-US.pdf", 2, "compass pulse AM3-18", "effective"),
    ("AeroCam Mini 3", "AeroCam-Mini-3_Release-Notes_v2.0_en-US.pdf", 2, "quiet rotor profile AM3-22", "effective"),
    ("AeroCam Mini 3", "AeroCam-Mini-3_配件指南_v1.0_zh-CN.pdf", 2, "磁吸护罩定位点", "effective"),
    ("AeroCam Mini 3", "AeroCam-Mini-3_快速入门_zh-CN.pdf", 2, "开机蜂鸣两次", "needs_review"),
    ("AeroCam Pro 3", "AeroCam-Pro-3_Service-Handbook_v2.0_en-US.pdf", 2, "optical sync AP3-20", "effective"),
    ("AeroCam Pro 3", "AeroCam-Pro-3_Release-Notes_v2.0_en-US.pdf", 2, "dual codec profile AP3-25", "effective"),
    ("AeroCam Pro 3", "AeroCam-Pro-3_配件指南_v1.0_zh-CN.pdf", 2, "专业云台扩展座", "effective"),
    ("AeroCam Pro 3", "AeroCam-Pro-3_快速入门_zh-CN.pdf", 2, "双击模式旋钮", "effective"),
]

OUTDATED = [
    ("AeroCam Mini 2", "AeroCam-Mini-2_Service-Handbook_v2.0_en-US.pdf", 2, "legacy horizon drift reset"),
    ("AeroCam Pro 2", "AeroCam-Pro-2_Service-Handbook_v3.0_en-US.pdf", 2, "legacy thermal alignment AP2-71"),
    ("AeroCam Mini 3", "AeroCam-Mini-3_Service-Handbook_v1.0_en-US.pdf", 2, "legacy compass pulse AM3-08"),
    ("AeroCam Pro 3", "AeroCam-Pro-3_Service-Handbook_v1.0_en-US.pdf", 2, "legacy optical sync AP3-10"),
]


def case(case_id, question, product, document, page, category, status,
         *, alias=False, should=True, notes=""):
    forbidden = sorted(set(ALIASES) - ({product} if product else set()))
    return {
        "id": case_id, "category": category, "question": question,
        "target_product": product, "allowed_products": [product] if product else [],
        "forbidden_products": forbidden, "target_document": document,
        "target_page": page, "expected_max_rank": 3,
        "should_return_answer": should,
        "allowed_document_statuses": [status] if status else [],
        "alias_expected": alias, "notes": notes,
    }


def build_cases():
    cases = []
    for index, (product, document, page, token, status) in enumerate(ACTIVE, start=1):
        short, chinese = ALIASES[product]
        cases.append(case(f"active-{index:02d}-fault", token, product, document, page,
                          "fault_without_product", status, notes="只描述故障或部件"))
        cases.append(case(f"active-{index:02d}-abbr", f"{short} {token}", product, document, page,
                          "product_abbreviation", status, alias=True, notes="缩写与故障词分离"))
        cases.append(case(f"active-{index:02d}-mixed", f"{chinese}，{token}", product, document, page,
                          "chinese_english_mixed", status, alias=True, notes="中英文混合和全角标点"))

    for index, (product, document, page, token) in enumerate(OUTDATED, start=1):
        short, chinese = ALIASES[product]
        for suffix, query in (("plain", token), ("abbr", f"{short} {token}"),
                              ("mixed", f"{chinese} {token}")):
            cases.append(case(f"outdated-{index:02d}-{suffix}", query, product, document, page,
                              "outdated_only", "superseded", alias=suffix != "plain",
                              notes="只能由已被替代文档回答，必须给出过期提示"))

    absent = [
        "AeroCam Mini 2 quantum toaster error", "ACP2 underwater coffee mode",
        "ACM3 teleport motor code Z-999", "航拍专业三代 月球网络认证失败",
        "不存在的产品 XQ-404 无法开机", "ACM2 not AP2-90 but unknown code",
        "如何绕过验证码抓取整站", "customer serial 0000 secret case",
        "AeroCam Mini 9 battery", "ACP3 orange nebula alarm",
        "Mini 2 与 Pro 3 同时出现但没有故障证据", "完全无关的天气查询",
    ]
    for index, query in enumerate(absent, start=1):
        cases.append(case(f"no-answer-{index:02d}", query, None, None, None,
                          "no_answer", None, should=False, notes="不存在答案时不得填充低相关结果"))

    extras = [
        ("typo-calibration", "ACM2 calbration", "AeroCam Mini 2", "AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf", 2, "common_typo"),
        ("typo-firmware", "ACM2 firmwere diagnostics", "AeroCam Mini 2", "AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf", 3, "common_typo"),
        ("fullwidth-model", "ＡＣＭ３ compass pulse AM3-18", "AeroCam Mini 3", "AeroCam-Mini-3_Service-Handbook_v2.0_en-US.pdf", 2, "fullwidth"),
        ("accessory-host-1", "ACM2 折叠支架锁扣", "AeroCam Mini 2", "AeroCam-Mini-2_配件指南_v1.1_zh-CN.pdf", 2, "accessory_vs_host"),
        ("accessory-host-2", "ACP2 双镜头保护框", "AeroCam Pro 2", "AeroCam-Pro-2_配件指南_v2.0_zh-CN.pdf", 2, "accessory_vs_host"),
        ("similar-mini2-mini3", "ACM2 battery handshake AM2-31", "AeroCam Mini 2", "AeroCam-Mini-2_Release-Notes_v3.0_en-US.pdf", 2, "similar_model"),
        ("similar-pro2-pro3", "ACP3 dual codec profile AP3-25", "AeroCam Pro 3", "AeroCam-Pro-3_Release-Notes_v2.0_en-US.pdf", 2, "similar_model"),
        ("multi-fault", "ACM3 compass pulse AM3-18", "AeroCam Mini 3", "AeroCam-Mini-3_Service-Handbook_v2.0_en-US.pdf", 2, "multi_fault"),
        ("negative-specific", "ACM2 gimbal home sensor", "AeroCam Mini 2", "AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf", 3, "negative_question"),
        ("casefold", "acp2 PAYLOAD FIRMWARE ap2-44", "AeroCam Pro 2", "AeroCam-Pro-2_Release-Notes_v4.0_en-US.pdf", 2, "casefold"),
        ("spaces", "  ACM3   quiet rotor profile AM3-22  ", "AeroCam Mini 3", "AeroCam-Mini-3_Release-Notes_v2.0_en-US.pdf", 2, "whitespace"),
        ("punctuation", "ACP3：optical sync AP3-20！", "AeroCam Pro 3", "AeroCam-Pro-3_Service-Handbook_v2.0_en-US.pdf", 2, "punctuation"),
    ]
    for case_id, query, product, document, page, category in extras:
        cases.append(case(case_id, query, product, document, page, category, "effective", alias=True))
    return cases


def main():
    cases = build_cases()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Created {len(cases)} cases in {OUTPUT}")


if __name__ == "__main__":
    main()
