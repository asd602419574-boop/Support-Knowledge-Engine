from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.dji_catalog import ensure_dji_products, load_dji_catalog
from support_knowledge_engine.importer import import_directory


def main() -> None:
    parser = argparse.ArgumentParser(
        description="导入 DJI 中国大陆官网下载清单、规范产品和 PDF 全文索引"
    )
    parser.add_argument("source", help="包含 manifest.json 与 files/ 的 DJI 资料目录")
    parser.add_argument(
        "--database",
        default=str(PROJECT_ROOT / "instance" / "knowledge.db"),
        help="SQLite 数据库路径",
    )
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve(strict=True)
    database = Path(args.database).expanduser().resolve()
    catalog = load_dji_catalog(source)
    if catalog is None:
        parser.error("指定目录未找到 DJI manifest.json")

    init_database(database)
    with connect_database(database) as connection:
        product_result = ensure_dji_products(connection, catalog)

    import_result = import_directory(source, database)
    with connect_database(database) as connection:
        counts = {
            "products": connection.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "documents": connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "pages": connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
            "authoritative_documents": connection.execute(
                "SELECT COUNT(*) FROM documents WHERE authority_level = 'authoritative'"
            ).fetchone()[0],
            "linked_documents": connection.execute(
                "SELECT COUNT(*) FROM documents WHERE canonical_product_id IS NOT NULL"
            ).fetchone()[0],
        }

    print(
        json.dumps(
            {
                "source": str(source),
                "database": str(database),
                "manifest": str(catalog.manifest_path),
                "manifest_documents": catalog.listed_documents,
                "manifest_failed_downloads": catalog.failed_downloads,
                "manifest_non_pdf_downloads": catalog.non_pdf_downloads,
                **product_result,
                "import": {
                    "run_id": import_result.run_id,
                    "discovered": import_result.discovered,
                    "imported": import_result.imported,
                    "duplicates": import_result.duplicates,
                    "failed": import_result.failed,
                },
                **counts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
