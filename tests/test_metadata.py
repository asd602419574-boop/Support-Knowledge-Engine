from __future__ import annotations

import unittest

from support_knowledge_engine.metadata import UNKNOWN, metadata_status, parse_metadata


class MetadataParsingTests(unittest.TestCase):
    def test_explicit_labels_are_extracted(self) -> None:
        text = """产品系列：星河路由器
产品型号：XR-100
文档类型：用户手册
语言：zh-CN
版本号：1.2
发布日期：2026/03/15
原始来源网址：https://example.com/manual
"""
        result = parse_metadata(text, {"title": "XR-100 用户手册"})

        self.assertEqual(result.title, "XR-100 用户手册")
        self.assertEqual(result.product_model, "XR-100")
        self.assertEqual(result.release_date, "2026-03-15")
        self.assertEqual(result.source_url, "https://example.com/manual")
        self.assertEqual(metadata_status(result), "已索引")

    def test_missing_or_invalid_values_remain_pending(self) -> None:
        text = """产品系列：通用实验设备
发布日期：大约在春季
原始来源网址：内部页面
正文提到型号 ABC-42，但没有明确的型号字段。
"""
        result = parse_metadata(text)

        self.assertEqual(result.product_model, UNKNOWN)
        self.assertEqual(result.release_date, UNKNOWN)
        self.assertEqual(result.source_url, UNKNOWN)
        self.assertEqual(metadata_status(result), "待确认")


if __name__ == "__main__":
    unittest.main()

