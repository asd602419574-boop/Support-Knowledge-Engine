from __future__ import annotations

from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "sample_docs"


SAMPLES = [
    {
        "filename": "星河路由器_XR-100_用户手册_v1.2_zh-CN.pdf",
        "title": "星河路由器 XR-100 用户手册",
        "font": "STSong-Light",
        "pages": [
            [
                "文档标题：星河路由器 XR-100 用户手册",
                "产品系列：星河路由器",
                "产品型号：XR-100",
                "文档类型：用户手册",
                "语言：zh-CN",
                "版本号：1.2",
                "发布日期：2026-03-15",
                "原始来源网址：https://example.com/support/xr-100/manual",
                "",
                "本手册使用完全虚构的产品名称和测试数据。",
                "第一章介绍设备接口、指示灯和本地配置步骤。",
            ],
            [
                "故障排查",
                "当状态灯连续闪烁三次时，请记录量子灯塔诊断码。",
                "断开电源十秒后重新连接，再检查本地管理页面。",
                "如果问题仍然存在，请保留本页页码与诊断码。",
            ],
        ],
    },
    {
        "filename": "Nebula-Switch_NS-24_Installation-Guide_v2.0_en-US.pdf",
        "title": "Nebula Switch NS-24 Installation Guide",
        "font": "Helvetica",
        "pages": [
            [
                "Document Title: Nebula Switch NS-24 Installation Guide",
                "Product Series: Nebula Switch",
                "Product Model: NS-24",
                "Document Type: Installation Guide",
                "Language: en-US",
                "Version: 2.0",
                "Release Date: 2026-04-02",
                "Source URL: https://example.com/support/ns-24/install",
                "",
                "This document uses fictional products and public example URLs.",
                "Mount the device with adequate clearance on every side.",
            ],
            [
                "Commissioning checklist",
                "Run uplink calibration before enabling the production profile.",
                "Confirm that port indicators remain stable for thirty seconds.",
                "Record the page number when reporting a failed check.",
            ],
        ],
    },
    {
        "filename": "通用故障排查指南_zh-CN.pdf",
        "title": "通用故障排查指南",
        "font": "STSong-Light",
        "pages": [
            [
                "文档标题：通用故障排查指南",
                "产品系列：通用实验设备",
                "文档类型：故障排查指南",
                "语言：zh-CN",
                "版本号：0.9",
                "发布日期：2026-05-08",
                "",
                "本测试文档故意省略产品型号和来源网址。",
                "系统应将缺失字段标记为待确认，不得根据正文猜测。",
            ],
            [
                "基础检查流程",
                "首先确认电源、连接线和本地状态指示。",
                "随后记录琥珀回声测试短语和当前页码。",
                "不要将未经核验的推测写入文档元数据。",
            ],
        ],
    },
]


def create_pdf(path: Path, title: str, pages: list[list[str]], font_name: str) -> None:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    document = canvas.Canvas(
        str(path), pagesize=(595.28, 841.89), pageCompression=1, invariant=1
    )
    document.setTitle(title)
    document.setAuthor("Support Knowledge Engine test fixture")
    document.setSubject("Synthetic PDF used for local import tests")

    for page_index, lines in enumerate(pages, start=1):
        document.setFont(font_name, 16)
        document.drawString(54, 790, title)
        document.setFont("STSong-Light", 10)
        document.setFillColorRGB(0.35, 0.39, 0.37)
        document.drawRightString(540, 792, f"{page_index} / {len(pages)}")
        document.setStrokeColorRGB(0.82, 0.85, 0.82)
        document.line(54, 772, 540, 772)
        document.setFillColorRGB(0.09, 0.13, 0.11)

        text = document.beginText(54, 744)
        text.setFont(font_name, 11)
        text.setLeading(21)
        for line in lines:
            text.textLine(line)
        document.drawText(text)

        document.setFont("STSong-Light", 8)
        document.setFillColorRGB(0.45, 0.49, 0.47)
        document.drawString(54, 40, "虚构测试文档 · 不包含公司或客户信息")
        document.showPage()

    document.save()


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for sample in SAMPLES:
        create_pdf(
            OUTPUT_DIR / sample["filename"],
            sample["title"],
            sample["pages"],
            sample["font"],
        )
    print(f"Created {len(SAMPLES)} sample PDFs in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
