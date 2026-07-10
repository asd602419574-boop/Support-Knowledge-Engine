from __future__ import annotations

import sqlite3


def filter_options(connection: sqlite3.Connection) -> dict[str, list[str]]:
    options: dict[str, list[str]] = {}
    for field in ("product_series", "document_type"):
        rows = connection.execute(
            f"SELECT DISTINCT {field} AS value FROM documents ORDER BY {field} COLLATE NOCASE"
        ).fetchall()
        options[field] = [row["value"] for row in rows]
    return options


def list_documents(connection: sqlite3.Connection, product_series: str = "",
                   document_type: str = "") -> list[sqlite3.Row]:
    conditions: list[str] = []
    parameters: list[str] = []
    if product_series:
        conditions.append("product_series = ?")
        parameters.append(product_series)
    if document_type:
        conditions.append("document_type = ?")
        parameters.append(document_type)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return connection.execute(
        f"""SELECT * FROM documents {where}
            ORDER BY imported_at DESC, filename COLLATE NOCASE""",
        parameters,
    ).fetchall()


def _fts_expression(query: str) -> str:
    terms = [term for term in query.split() if term]
    if not terms:
        terms = [query]
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def search_documents(connection: sqlite3.Connection, query: str,
                     product_series: str = "", document_type: str = "") -> list[sqlite3.Row]:
    query = query.strip()
    if not query:
        return []

    conditions: list[str] = []
    filter_parameters: list[str] = []
    if product_series:
        conditions.append("d.product_series = ?")
        filter_parameters.append(product_series)
    if document_type:
        conditions.append("d.document_type = ?")
        filter_parameters.append(document_type)
    extra_where = f" AND {' AND '.join(conditions)}" if conditions else ""

    terms = [term for term in query.split() if term] or [query]
    if all(len(term) >= 3 for term in terms):
        return connection.execute(
            f"""SELECT d.*, page_fts.page_number,
                       snippet(page_fts, 0, '', '', ' … ', 24) AS snippet,
                       bm25(page_fts) AS rank
                FROM page_fts
                JOIN documents d ON d.id = page_fts.document_id
                WHERE page_fts MATCH ? {extra_where}
                ORDER BY rank, d.filename COLLATE NOCASE, page_fts.page_number""",
            [_fts_expression(query), *filter_parameters],
        ).fetchall()

    like_value = f"%{query.replace('%', r'\%').replace('_', r'\_')}%"
    return connection.execute(
        f"""SELECT d.*, p.page_number,
                   substr(p.content, max(instr(lower(p.content), lower(?)) - 48, 1), 180) AS snippet,
                   0 AS rank
            FROM pages p
            JOIN documents d ON d.id = p.document_id
            WHERE p.content LIKE ? ESCAPE '\\' {extra_where}
            ORDER BY d.filename COLLATE NOCASE, p.page_number""",
        [query, like_value, *filter_parameters],
    ).fetchall()


def get_document(connection: sqlite3.Connection, document_id: int) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()


def get_document_pages(connection: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT page_number, content FROM pages WHERE document_id = ? ORDER BY page_number",
        (document_id,),
    ).fetchall()


def get_import_runs(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM import_runs ORDER BY id DESC LIMIT 100"
    ).fetchall()


def get_import_items(connection: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    rows = connection.execute(
        "SELECT * FROM import_items ORDER BY id DESC LIMIT 1000"
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["run_id"], []).append(row)
    return grouped

