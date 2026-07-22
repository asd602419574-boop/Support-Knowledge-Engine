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
    {
        "standard_name": "AeroCam Mini 3",
        "product_series": "AeroCam",
        "status": "active",
        "aliases": (
            ("Aero Mini 3", "english_name"),
            ("ACM3", "abbreviation"),
            ("航拍迷你三代", "chinese_name"),
        ),
    },
    {
        "standard_name": "AeroCam Pro 3",
        "product_series": "AeroCam",
        "status": "active",
        "aliases": (
            ("Aero Pro 3", "english_name"),
            ("ACP3", "abbreviation"),
            ("航拍专业三代", "chinese_name"),
        ),
    },
)


SUPERSESSION_PAIRS = (
    ("AeroCam-Mini-2_Service-Handbook_v2.0_en-US.pdf", "AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf"),
    ("AeroCam-Pro-2_Service-Handbook_v3.0_en-US.pdf", "AeroCam-Pro-2_Service-Handbook_v4.0_en-US.pdf"),
    ("AeroCam-Mini-3_Service-Handbook_v1.0_en-US.pdf", "AeroCam-Mini-3_Service-Handbook_v2.0_en-US.pdf"),
    ("AeroCam-Pro-3_Service-Handbook_v1.0_en-US.pdf", "AeroCam-Pro-3_Service-Handbook_v2.0_en-US.pdf"),
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

        supersession_count = 0
        for old_filename, new_filename in SUPERSESSION_PAIRS:
            old = connection.execute("SELECT id FROM documents WHERE filename = ?", (old_filename,)).fetchone()
            new = connection.execute("SELECT id FROM documents WHERE filename = ?", (new_filename,)).fetchone()
            if old and new:
                connection.execute(
                    """UPDATE documents SET status = 'superseded', superseded_by_document_id = ?,
                       expiration_date = COALESCE(expiration_date, '2026-06-30'),
                       status_note = '虚构语料版本替代关系' WHERE id = ?""",
                    (new["id"], old["id"]),
                )
                connection.execute(
                    """UPDATE documents SET status = 'effective', effective_date = COALESCE(effective_date, release_date)
                       WHERE id = ?""",
                    (new["id"],),
                )
                supersession_count += 1

        counts = {
            "products": connection.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "aliases": connection.execute("SELECT COUNT(*) FROM product_aliases").fetchone()[0],
            "linked_documents": linked_documents,
            "supersession_relationships": supersession_count,
        }
    return counts
