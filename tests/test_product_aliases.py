from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.governance import (
    add_product_alias,
    create_product,
    match_product_alias,
)


class ProductAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "aliases.db"
        init_database(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_multiple_names_map_to_one_product_case_insensitively(self) -> None:
        with connect_database(self.database_path) as connection:
            product_id = create_product(
                connection,
                {"standard_name": "AeroCam Mini 2", "product_series": "AeroCam", "status": "active"},
                "建立虚构测试产品",
                "测试维护者",
            )
            for alias, alias_type in (
                ("Aero Mini 2", "english_name"),
                ("ACM2", "abbreviation"),
                ("航拍迷你二代", "chinese_name"),
            ):
                add_product_alias(connection, product_id, alias, alias_type, "添加测试别名", "测试维护者")

            matches = [
                match_product_alias(connection, name)
                for name in ("AeroCam Mini 2", "aero mini 2", "acm2", "航拍迷你二代")
            ]

        self.assertTrue(all(match.status == "matched" for match in matches))
        self.assertEqual({match.product_id for match in matches}, {product_id})

    def test_same_alias_on_two_products_is_reported_as_conflict(self) -> None:
        with connect_database(self.database_path) as connection:
            first = create_product(
                connection,
                {"standard_name": "AeroCam Mini 2", "product_series": "AeroCam", "status": "active"},
                "建立产品一",
                "测试维护者",
            )
            second = create_product(
                connection,
                {"standard_name": "AeroCam Micro 2", "product_series": "AeroCam", "status": "active"},
                "建立产品二",
                "测试维护者",
            )
            add_product_alias(connection, first, "ACM2", "abbreviation", "添加缩写", "测试维护者")
            _, conflict = add_product_alias(
                connection, second, "acm2", "abbreviation", "验证冲突", "测试维护者"
            )
            result = match_product_alias(connection, "ACM2")

        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(result.status, "conflict")
        self.assertEqual(set(result.product_ids), {first, second})


if __name__ == "__main__":
    unittest.main()
