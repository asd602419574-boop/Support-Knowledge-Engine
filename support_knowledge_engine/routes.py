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
from .governance import (
    ValidationError,
    add_product_alias,
    create_product,
    set_alias_enabled,
    update_document,
    update_product,
)
from .importer import calculate_sha256, import_directory
from .repository import (
    alias_conflict_products,
    audit_filter_options,
    filter_options,
    get_audit_log,
    get_document,
    get_document_field_values,
    get_document_pages,
    get_import_items,
    get_import_runs,
    get_product,
    get_product_aliases,
    get_product_documents,
    get_replacement_candidates,
    get_source_fetches,
    list_documents,
    list_products,
    search_with_context,
)


bp = Blueprint("main", __name__)


def _database_path() -> str:
    return current_app.config["DATABASE"]


def _operator_name() -> str:
    return current_app.config["OPERATOR_NAME"]


def _flash_validation_error(error: ValidationError) -> None:
    for message in error.errors.values():
        flash(message, "error")


@bp.get("/")
def index():
    query = request.args.get("q", "").strip()
    product_series = request.args.get("product_series", "").strip()
    document_type = request.args.get("document_type", "").strip()
    status = request.args.get("status", "").strip()
    association = request.args.get("association", "").strip()
    product_id = request.args.get("product_id", "").strip()
    with connect_database(_database_path()) as connection:
        options = filter_options(connection)
        if query:
            search_context = search_with_context(
                connection,
                query,
                product_series,
                document_type,
                status,
                association,
                product_id,
            )
            results = search_context["results"]
            alias_conflicts = alias_conflict_products(connection, query)
            documents = []
        else:
            documents = list_documents(
                connection,
                product_series,
                document_type,
                status,
                association,
                product_id,
            )
            alias_conflicts = []
            results = []
            search_context = None

    return render_template(
        "index.html",
        query=query,
        product_series=product_series,
        document_type=document_type,
        status=status,
        association=association,
        product_id=product_id,
        options=options,
        documents=documents,
        results=results,
        alias_conflicts=alias_conflicts,
        search_context=search_context,
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


def _document_page_context(document_id: int) -> dict[str, object]:
    with connect_database(_database_path()) as connection:
        document = get_document(connection, document_id)
        if document is None:
            abort(404)
        return {
            "document": document,
            "pages": get_document_pages(connection, document_id),
            "field_values": get_document_field_values(connection, document_id),
            "products": list_products(connection),
            "replacement_candidates": get_replacement_candidates(connection, document_id),
            "audit_entries": get_audit_log(
                connection, object_type="document", object_id=document_id, limit=100
            ),
        }


@bp.get("/documents/<int:document_id>")
def document_detail(document_id: int):
    return render_template("document.html", **_document_page_context(document_id))


@bp.post("/documents/<int:document_id>/edit")
def edit_document(document_id: int):
    values = request.form.to_dict()
    reason = request.form.get("reason", "")
    try:
        with connect_database(_database_path()) as connection:
            changed = update_document(
                connection, document_id, values, reason, _operator_name()
            )
        flash(f"已保存 {changed} 项修改，并写入审计日志。", "success")
    except ValidationError as exc:
        _flash_validation_error(exc)
    return redirect(url_for("main.document_detail", document_id=document_id))


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
        source_fetches = get_source_fetches(connection)
    return render_template("logs.html", runs=runs, items=items, source_fetches=source_fetches)


@bp.get("/products")
def products():
    with connect_database(_database_path()) as connection:
        product_rows = list_products(connection)
    return render_template("products.html", products=product_rows)


@bp.post("/products")
def create_product_route():
    try:
        with connect_database(_database_path()) as connection:
            product_id = create_product(
                connection, request.form.to_dict(), request.form.get("reason", ""), _operator_name()
            )
        flash("规范产品已创建，标准名称同时加入别名库。", "success")
        return redirect(url_for("main.product_detail", product_id=product_id))
    except ValidationError as exc:
        _flash_validation_error(exc)
        return redirect(url_for("main.products"))


@bp.get("/products/<int:product_id>")
def product_detail(product_id: int):
    with connect_database(_database_path()) as connection:
        product = get_product(connection, product_id)
        if not product:
            abort(404)
        aliases = get_product_aliases(connection, product_id)
        documents = get_product_documents(connection, product_id)
        audit_entries = get_audit_log(
            connection, object_type="product", object_id=product_id, limit=100
        )
    return render_template(
        "product.html",
        product=product,
        aliases=aliases,
        documents=documents,
        audit_entries=audit_entries,
    )


@bp.post("/products/<int:product_id>/edit")
def edit_product_route(product_id: int):
    try:
        with connect_database(_database_path()) as connection:
            changed = update_product(
                connection,
                product_id,
                request.form.to_dict(),
                request.form.get("reason", ""),
                _operator_name(),
            )
        flash(f"已保存 {changed} 项产品修改。", "success")
    except ValidationError as exc:
        _flash_validation_error(exc)
    return redirect(url_for("main.product_detail", product_id=product_id))


@bp.post("/products/<int:product_id>/aliases")
def add_alias_route(product_id: int):
    try:
        with connect_database(_database_path()) as connection:
            _, match = add_product_alias(
                connection,
                product_id,
                request.form.get("alias_text", ""),
                request.form.get("alias_type", ""),
                request.form.get("reason", ""),
                _operator_name(),
            )
        if match.status == "conflict":
            flash("别名已保存，但它同时匹配多个产品，已标记为冲突，自动关联将暂停。", "warning")
        else:
            flash("产品别名已添加。", "success")
    except ValidationError as exc:
        _flash_validation_error(exc)
    return redirect(url_for("main.product_detail", product_id=product_id))


@bp.post("/aliases/<int:alias_id>/toggle")
def toggle_alias_route(alias_id: int):
    product_id = request.form.get("product_id", "")
    if not product_id.isdigit():
        abort(400)
    try:
        with connect_database(_database_path()) as connection:
            set_alias_enabled(
                connection,
                alias_id,
                request.form.get("enabled") == "1",
                request.form.get("reason", ""),
                _operator_name(),
            )
        flash("别名状态已更新。", "success")
    except ValidationError as exc:
        _flash_validation_error(exc)
    return redirect(url_for("main.product_detail", product_id=int(product_id)))


@bp.get("/audit")
def audit_log():
    object_type = request.args.get("object_type", "").strip()
    field_name = request.args.get("field_name", "").strip()
    operator = request.args.get("operator", "").strip()
    with connect_database(_database_path()) as connection:
        entries = get_audit_log(
            connection,
            object_type=object_type,
            field_name=field_name,
            operator=operator,
        )
        options = audit_filter_options(connection)
    return render_template(
        "audit.html",
        entries=entries,
        options=options,
        object_type=object_type,
        field_name=field_name,
        operator=operator,
    )
