from __future__ import annotations

import sqlite3
from pathlib import Path

from .db import connect_database, init_database
from .governance import (
    add_product_alias,
    create_product,
    match_document_product,
    normalize_alias,
)
from .importer import import_directory


DEMO_OPERATOR = "虚构数据初始化器"
DEMO_REASON = "建立第二阶段虚构产品治理与检索评测数据"


DEMO_PRODUCTS = (
    {
        "standard_name": "AeroCam Mini 2",
        "product_series": "AeroCam",
        "status": "active",
        "aliases": (
            ("Aero Mini 2", "english_name"),
            ("ACM2", "abbreviation"),
            ("航拍迷你二代", "chinese_name"),
        ),
    },
    {
        "standard_name": "AeroCam Pro 2",
        "product_series": "AeroCam",
        "status": "active",
        "aliases": (
            ("Aero Pro 2", "english_name"),
            ("ACP2", "abbreviation"),
            ("航拍专业二代", "chinese_name"),
        ),
    },
)


def _ensure_product(connection: sqlite3.Connection, definition: dict) -> int:
    existing = connection.execute(
        "SELECT id FROM products WHERE standard_name = ? COLLATE NOCASE",
        (definition["standard_name"],),
    ).fetchone()
    if existing:
        product_id = existing["id"]
    else:
        product_id = create_product(
            connection, definition, DEMO_REASON, DEMO_OPERATOR
        )

    for alias_text, alias_type in definition["aliases"]:
        existing_alias = connection.execute(
            """SELECT 1 FROM product_aliases
               WHERE product_id = ? AND normalized_alias = ?""",
            (product_id, normalize_alias(alias_text)),
        ).fetchone()
        if not existing_alias:
            add_product_alias(
                connection,
                product_id,
                alias_text,
                alias_type,
                DEMO_REASON,
                DEMO_OPERATOR,
            )
    return product_id


def seed_demo_data(
    database_path: str | Path,
    sample_directory: str | Path | None = None,
) -> dict[str, int]:
    init_database(database_path)
    if sample_directory is not None:
        import_directory(sample_directory, database_path)

    with connect_database(database_path) as connection:
        for definition in DEMO_PRODUCTS:
            _ensure_product(connection, definition)

        linked_documents = 0
        documents = connection.execute(
            """SELECT id, title, product_series, product_model
               FROM documents WHERE canonical_product_id IS NULL"""
        ).fetchall()
        for document in documents:
            match = match_document_product(connection, dict(document))
            if match.status == "matched" and match.product_id is not None:
                connection.execute(
                    "UPDATE documents SET canonical_product_id = ? WHERE id = ?",
                    (match.product_id, document["id"]),
                )
                linked_documents += 1

        counts = {
            "products": connection.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "aliases": connection.execute("SELECT COUNT(*) FROM product_aliases").fetchone()[0],
            "linked_documents": linked_documents,
        }
    return counts
