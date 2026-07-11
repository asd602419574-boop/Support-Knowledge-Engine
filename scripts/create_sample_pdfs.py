from __future__ import annotations

from pathlib import Path
import shutil

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
    {
        "filename": "AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf",
        "title": "AeroCam Mini 2 Service Handbook",
        "font": "Helvetica",
        "pages": [
            [
                "Document Title: AeroCam Mini 2 Service Handbook",
                "Product Series: AeroCam",
                "Product Model: AeroCam Mini 2",
                "Document Type: Service Handbook",
                "Language: en-US",
                "Version: 3.1",
                "Release Date: 2026-06-12",
                "Source URL: https://example.com/support/aerocam-mini-2/service",
                "",
                "A fictional compact aerial imaging product used only for governance tests.",
                "Use the model identifier before selecting a maintenance procedure.",
            ],
            [
                "Flight system maintenance",
                "Run propeller calibration after replacing a motor or vibration damper.",
                "Inspect the battery latch before enabling coastal wind compensation.",
                "Record the calibration result together with this page number.",
            ],
            [
                "Firmware diagnostics",
                "Supported firmware range: 2.4.0 through 2.9.x.",
                "Error code AM2-17 indicates that the gimbal home sensor needs inspection.",
                "Do not apply procedures written for AeroCam Pro 2.",
            ],
        ],
    },
    {
        "filename": "AeroCam-Pro-2_Service-Handbook_v4.0_en-US.pdf",
        "title": "AeroCam Pro 2 Service Handbook",
        "font": "Helvetica",
        "pages": [
            [
                "Document Title: AeroCam Pro 2 Service Handbook",
                "Product Series: AeroCam",
                "Product Model: AeroCam Pro 2",
                "Document Type: Service Handbook",
                "Language: en-US",
                "Version: 4.0",
                "Release Date: 2026-06-20",
                "Source URL: https://example.com/support/aerocam-pro-2/service",
                "",
                "This fictional professional model is intentionally easy to confuse with Mini 2.",
                "Verify the full standard product name before servicing the payload system.",
            ],
            [
                "Professional payload diagnostics",
                "Error code AP2-90 indicates a dual-camera thermal alignment failure.",
                "Inspect the payload bay connector and repeat optical axis verification.",
                "Mini-series calibration instructions are not compatible with this model.",
            ],
        ],
    },
]


def english_sample(filename: str, product: str, document_type: str, version: str,
                   date: str, token: str, *, missing: str = "") -> dict:
    metadata = [
        f"Document Title: {product} {document_type}",
        "Product Series: AeroCam",
        f"Product Model: {product}",
        f"Document Type: {document_type}",
        "Language: en-US",
        f"Version: {version}",
        f"Release Date: {date}",
        f"Source URL: https://example.com/support/{filename[:-4].lower()}",
    ]
    labels = {
        "model": "Product Model:", "version": "Version:", "date": "Release Date:",
        "source": "Source URL:",
    }
    if missing in labels:
        metadata = [line for line in metadata if not line.startswith(labels[missing])]
    return {
        "filename": filename,
        "title": f"{product} {document_type}",
        "font": "Helvetica",
        "pages": [
            [*metadata, "", "Synthetic corpus document. No company or customer information."],
            [f"Support topic: {token}", f"Procedure marker: {token} verified workflow.",
             f"This procedure applies only to {product}; do not substitute a similar model."],
        ],
    }


def chinese_sample(filename: str, product: str, title: str, version: str,
                   date: str, token: str, *, missing: str = "") -> dict:
    metadata = [
        f"文档标题：{product} {title}", "产品系列：AeroCam", f"产品型号：{product}",
        f"文档类型：{title}", "语言：zh-CN", f"版本号：{version}",
        f"发布日期：{date}", f"原始来源网址：https://example.com/support/{filename[:-4]}",
    ]
    labels = {"model": "产品型号：", "version": "版本号：", "date": "发布日期：", "source": "原始来源网址："}
    if missing in labels:
        metadata = [line for line in metadata if not line.startswith(labels[missing])]
    return {
        "filename": filename, "title": f"{product} {title}", "font": "STSong-Light",
        "pages": [
            [*metadata, "", "本文件为虚构语料，不含公司或客户资料。"],
            [f"支持主题：{token}", f"处理步骤：执行 {token} 检查流程。",
             f"本流程仅适用于 {product}，不得与相似型号混用。"],
        ],
    }


SAMPLES.extend([
    english_sample("AeroCam-Mini-2_Service-Handbook_v2.0_en-US.pdf", "AeroCam Mini 2", "Service Handbook", "2.0", "2025-02-10", "legacy horizon drift reset"),
    english_sample("AeroCam-Mini-2_Release-Notes_v3.0_en-US.pdf", "AeroCam Mini 2", "Release Notes", "3.0", "2026-05-02", "battery handshake AM2-31"),
    chinese_sample("AeroCam-Mini-2_配件指南_v1.1_zh-CN.pdf", "AeroCam Mini 2", "配件指南", "1.1", "2026-04-03", "折叠支架锁扣"),
    chinese_sample("AeroCam-Mini-2_快速入门_zh-CN.pdf", "AeroCam Mini 2", "快速入门", "1.0", "2026-01-06", "首次配对蓝灯", missing="source"),
    english_sample("AeroCam-Pro-2_Service-Handbook_v3.0_en-US.pdf", "AeroCam Pro 2", "Service Handbook", "3.0", "2025-03-11", "legacy thermal alignment AP2-71"),
    english_sample("AeroCam-Pro-2_Release-Notes_v4.0_en-US.pdf", "AeroCam Pro 2", "Release Notes", "4.0", "2026-06-21", "payload firmware AP2-44"),
    chinese_sample("AeroCam-Pro-2_配件指南_v2.0_zh-CN.pdf", "AeroCam Pro 2", "配件指南", "2.0", "2026-04-08", "双镜头保护框"),
    chinese_sample("AeroCam-Pro-2_快速入门_zh-CN.pdf", "AeroCam Pro 2", "快速入门", "1.0", "2026-02-09", "载荷舱解锁", missing="date"),
    english_sample("AeroCam-Mini-3_Service-Handbook_v1.0_en-US.pdf", "AeroCam Mini 3", "Service Handbook", "1.0", "2025-06-01", "legacy compass pulse AM3-08"),
    english_sample("AeroCam-Mini-3_Service-Handbook_v2.0_en-US.pdf", "AeroCam Mini 3", "Service Handbook", "2.0", "2026-06-01", "compass pulse AM3-18"),
    english_sample("AeroCam-Mini-3_Release-Notes_v2.0_en-US.pdf", "AeroCam Mini 3", "Release Notes", "2.0", "2026-06-02", "quiet rotor profile AM3-22"),
    chinese_sample("AeroCam-Mini-3_配件指南_v1.0_zh-CN.pdf", "AeroCam Mini 3", "配件指南", "1.0", "2026-03-12", "磁吸护罩定位点"),
    chinese_sample("AeroCam-Mini-3_快速入门_zh-CN.pdf", "AeroCam Mini 3", "快速入门", "1.0", "2026-03-01", "开机蜂鸣两次", missing="version"),
    english_sample("AeroCam-Pro-3_Service-Handbook_v1.0_en-US.pdf", "AeroCam Pro 3", "Service Handbook", "1.0", "2025-07-01", "legacy optical sync AP3-10"),
    english_sample("AeroCam-Pro-3_Service-Handbook_v2.0_en-US.pdf", "AeroCam Pro 3", "Service Handbook", "2.0", "2026-07-01", "optical sync AP3-20"),
    english_sample("AeroCam-Pro-3_Release-Notes_v2.0_en-US.pdf", "AeroCam Pro 3", "Release Notes", "2.0", "2026-07-02", "dual codec profile AP3-25"),
    chinese_sample("AeroCam-Pro-3_配件指南_v1.0_zh-CN.pdf", "AeroCam Pro 3", "配件指南", "1.0", "2026-03-18", "专业云台扩展座"),
    chinese_sample("AeroCam-Pro-3_快速入门_zh-CN.pdf", "AeroCam Pro 3", "快速入门", "1.0", "2026-03-20", "双击模式旋钮"),
])


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
    # 两个文件名不同但字节完全相同的副本，用于验证 SHA-256 去重。
    shutil.copyfile(
        OUTPUT_DIR / "AeroCam-Mini-3_Release-Notes_v2.0_en-US.pdf",
        OUTPUT_DIR / "Z-copy_AeroCam-Mini-3_Release-Notes.pdf",
    )
    shutil.copyfile(
        OUTPUT_DIR / "AeroCam-Pro-3_配件指南_v1.0_zh-CN.pdf",
        OUTPUT_DIR / "Z-copy_AeroCam-Pro-3_附件说明.pdf",
    )
    print(f"Created {len(SAMPLES) + 2} sample PDF files ({len(SAMPLES)} unique) in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
