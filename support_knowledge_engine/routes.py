from __future__ import annotations

from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .db import connect_database
from .importer import calculate_sha256, import_directory
from .repository import (
    filter_options,
    get_document,
    get_document_pages,
    get_import_items,
    get_import_runs,
    list_documents,
    search_documents,
)


bp = Blueprint("main", __name__)


def _database_path() -> str:
    return current_app.config["DATABASE"]


@bp.get("/")
def index():
    query = request.args.get("q", "").strip()
    product_series = request.args.get("product_series", "").strip()
    document_type = request.args.get("document_type", "").strip()
    with connect_database(_database_path()) as connection:
        options = filter_options(connection)
        if query:
            results = search_documents(connection, query, product_series, document_type)
            documents = []
        else:
            documents = list_documents(connection, product_series, document_type)
            results = []

    return render_template(
        "index.html",
        query=query,
        product_series=product_series,
        document_type=document_type,
        options=options,
        documents=documents,
        results=results,
    )


@bp.post("/imports")
def start_import():
    directory = request.form.get("directory", "").strip()
    if not directory:
        flash("请指定要扫描的本地目录。", "error")
        return redirect(url_for("main.index"))
    try:
        summary = import_directory(directory, _database_path())
        category = "warning" if summary.failed else "success"
        flash(
            f"扫描 {summary.discovered} 个 PDF：导入 {summary.imported}，"
            f"重复 {summary.duplicates}，失败 {summary.failed}。",
            category,
        )
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.logs"))


@bp.get("/documents/<int:document_id>")
def document_detail(document_id: int):
    with connect_database(_database_path()) as connection:
        document = get_document(connection, document_id)
        if document is None:
            abort(404)
        pages = get_document_pages(connection, document_id)
    return render_template("document.html", document=document, pages=pages)


@bp.get("/documents/<int:document_id>/file")
def document_file(document_id: int):
    with connect_database(_database_path()) as connection:
        document = get_document(connection, document_id)
    if document is None:
        abort(404)

    file_path = Path(document["file_path"])
    if not file_path.is_file():
        abort(404, "原始 PDF 已移动或删除")
    if calculate_sha256(file_path) != document["sha256"]:
        abort(409, "原始 PDF 内容已变化，请重新导入后再查看")
    return send_file(file_path, mimetype="application/pdf", as_attachment=False)


@bp.get("/logs")
def logs():
    with connect_database(_database_path()) as connection:
        runs = get_import_runs(connection)
        items = get_import_items(connection)
    return render_template("logs.html", runs=runs, items=items)

