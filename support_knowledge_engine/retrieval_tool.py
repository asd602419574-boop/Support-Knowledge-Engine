from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Mapping

from . import repository
from .search_telemetry import (
    DEFAULT_TELEMETRY_DEADLINE_S,
    TelemetrySink,
    emit_search_telemetry,
)

TOOL_NAME = "knowledge_store_retrieval"
TOOL_VERSION = "1"
REQUEST_SCHEMA_VERSION = "1"
RESPONSE_SCHEMA_VERSION = "1"

ERROR_INVALID_REQUEST = "invalid_request"
ERROR_RETRIEVAL_FAILURE = "retrieval_failure"
ERROR_RETRIEVAL_TIMEOUT = "retrieval_timeout"

MAX_RESULTS = 100
MAX_SNIPPET_CHARS = 4000
MAX_QUERY_CHARS = 2000
MAX_FILTER_CHARS = 200
DEFAULT_RETRIEVAL_DEADLINE_S = 5.0
MAX_RETRIEVAL_DEADLINE_S = 30.0
MAX_TELEMETRY_DEADLINE_S = 5.0

_ASSOCIATIONS = {"", "linked", "unlinked"}
_REQUEST_FIELDS = {
    "request_schema_version",
    "query",
    "product_id",
    "product_series",
    "document_type",
    "status",
    "association",
    "max_results",
    "max_snippet_chars",
}


class _InvalidRequest(ValueError):
    pass


class _RetrievalTimeout(Exception):
    pass


def execute_retrieval_tool(
    connection: sqlite3.Connection,
    request: object,
    *,
    telemetry_sink: TelemetrySink | None = None,
    telemetry_deadline_s: float = DEFAULT_TELEMETRY_DEADLINE_S,
    retrieval_deadline_s: float = DEFAULT_RETRIEVAL_DEADLINE_S,
) -> dict[str, object]:
    """Run one shared retrieval, then bounded class-C telemetry.

    The retrieval deadline aborts the in-flight SQLite statement. It is not an
    elapsed check after the statement returns. The default deadline is finite.
    The presentation limit is applied to the returned copy and does not start
    another retrieval or change match_state.
    """
    try:
        params = _validate_request(request)
        _validate_deadline(telemetry_deadline_s, "telemetry_deadline_s", MAX_TELEMETRY_DEADLINE_S)
        deadline_s = _validate_deadline(
            retrieval_deadline_s, "retrieval_deadline_s", MAX_RETRIEVAL_DEADLINE_S
        )
    except _InvalidRequest as exc:
        return _envelope(ok=False, error=_error(ERROR_INVALID_REQUEST, str(exc)), telemetry=None, result=None)

    try:
        core_result = _retrieve_within_deadline(
            connection,
            lambda: repository.retrieve_with_context(
                connection,
                params["query"],
                params["product_series"],
                params["document_type"],
                params["status"],
                params["association"],
                params["product_id"],
            ),
            deadline_s,
        )
    except _RetrievalTimeout:
        return _envelope(
            ok=False,
            error=_error(ERROR_RETRIEVAL_TIMEOUT, "检索超过时限。"),
            telemetry=None,
            result=None,
        )
    except Exception:
        return _envelope(
            ok=False,
            error=_error(ERROR_RETRIEVAL_FAILURE, "检索执行失败。"),
            telemetry=None,
            result=None,
        )

    presented = _present(
        core_result,
        max_results=params["max_results"],
        max_snippet_chars=params["max_snippet_chars"],
    )
    telemetry = emit_search_telemetry(
        connection,
        core_result,
        sink=telemetry_sink,
        deadline_s=telemetry_deadline_s,
    )
    return _envelope(ok=True, error=None, telemetry=telemetry.as_dict(), result=presented)


def _retrieve_within_deadline(
    connection: sqlite3.Connection,
    retrieve: Callable[[], dict[str, object]],
    deadline_s: float,
) -> dict[str, object]:
    """Abort the caller's current SQLite work when the deadline is reached.

    busy_timeout bounds a lock wait. sqlite3_interrupt and the progress handler
    abort a statement that is still executing. The watchdog is joined before
    this function returns, so it does not keep the caller connection afterward.
    """
    deadline_at = time.monotonic() + deadline_s
    previous_timeout = _read_busy_timeout(connection)
    cancel = threading.Event()
    progress_installed = False

    def _watchdog() -> None:
        remaining = deadline_at - time.monotonic()
        if remaining > 0 and cancel.wait(remaining):
            return
        try:
            connection.interrupt()
        except sqlite3.Error:
            pass

    def _progress() -> int:
        return 1 if time.monotonic() >= deadline_at else 0

    watchdog = threading.Thread(target=_watchdog, name="retrieval-deadline", daemon=True)
    watchdog_started = False
    try:
        connection.execute(f"PRAGMA busy_timeout = {max(1, int(deadline_s * 1000))}")
        if hasattr(connection, "set_progress_handler"):
            connection.set_progress_handler(_progress, 10000)
            progress_installed = True
        watchdog.start()
        watchdog_started = True
        return retrieve()
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "interrupted" in message or "locked" in message:
            raise _RetrievalTimeout from exc
        raise
    finally:
        cancel.set()
        if watchdog_started:
            watchdog.join(timeout=1.0)
        if progress_installed:
            connection.set_progress_handler(None, 0)
        _drain_interrupt(connection)
        if previous_timeout is not None:
            try:
                connection.execute(f"PRAGMA busy_timeout = {previous_timeout}")
            except sqlite3.Error:
                pass


def _read_busy_timeout(connection: sqlite3.Connection) -> int | None:
    try:
        row = connection.execute("PRAGMA busy_timeout").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return int(row[0])


def _drain_interrupt(connection: sqlite3.Connection) -> None:
    for _ in range(3):
        try:
            connection.execute("SELECT 1").fetchone()
            return
        except sqlite3.Error:
            continue


def _validate_request(request: object) -> dict[str, object]:
    if not isinstance(request, dict):
        raise _InvalidRequest("请求必须是对象。")
    unknown = set(request) - _REQUEST_FIELDS
    if unknown:
        raise _InvalidRequest("请求包含未声明的字段。")
    if request.get("request_schema_version") != REQUEST_SCHEMA_VERSION:
        raise _InvalidRequest("request_schema_version 不受支持。")
    if "query" not in request:
        raise _InvalidRequest("query 不能缺少。")
    query = request["query"]
    if not isinstance(query, str):
        raise _InvalidRequest("query 必须是字符串。")
    if len(query) > MAX_QUERY_CHARS:
        raise _InvalidRequest("query 超出允许范围。")
    return {
        "query": query,
        "product_id": _product_id(request),
        "product_series": _optional_text(request, "product_series"),
        "document_type": _optional_text(request, "document_type"),
        "status": _optional_text(request, "status", maximum=64),
        "association": _association(request),
        "max_results": _optional_bound(request, "max_results", MAX_RESULTS),
        "max_snippet_chars": _optional_bound(request, "max_snippet_chars", MAX_SNIPPET_CHARS),
    }


def _optional_text(request: Mapping[str, object], field: str, maximum: int = MAX_FILTER_CHARS) -> str:
    if field not in request:
        return ""
    value = request[field]
    if not isinstance(value, str):
        raise _InvalidRequest(f"{field} 必须是字符串。")
    if len(value) > maximum:
        raise _InvalidRequest(f"{field} 超出允许范围。")
    return value


def _product_id(request: Mapping[str, object]) -> str:
    value = _optional_text(request, "product_id", maximum=32)
    if value and not value.isdigit():
        raise _InvalidRequest("product_id 必须是数字。")
    return value


def _association(request: Mapping[str, object]) -> str:
    value = _optional_text(request, "association", maximum=32)
    if value not in _ASSOCIATIONS:
        raise _InvalidRequest("association 不受支持。")
    return value


def _optional_bound(request: Mapping[str, object], field: str, maximum: int) -> int | None:
    if field not in request or request[field] is None:
        return None
    value = request[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _InvalidRequest(f"{field} 必须是整数。")
    if value < 1 or value > maximum:
        raise _InvalidRequest(f"{field} 超出允许范围。")
    return value


def _validate_deadline(value: object, field: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidRequest(f"{field} 必须是数字。")
    if value <= 0 or value > maximum:
        raise _InvalidRequest(f"{field} 超出允许范围。")
    return float(value)


def _present(
    result: Mapping[str, object],
    *,
    max_results: int | None,
    max_snippet_chars: int | None,
) -> dict[str, object]:
    presented = dict(result)
    rows = list(result["results"])
    if max_results is not None:
        rows = rows[:max_results]
    limited_rows = []
    for row in rows:
        item = dict(row)
        snippet = item.get("snippet")
        if max_snippet_chars is not None and isinstance(snippet, str):
            item["snippet"] = snippet[:max_snippet_chars]
        limited_rows.append(item)
    presented["results"] = limited_rows
    return presented


def _error(error_type: str, message: str) -> dict[str, str]:
    return {"type": error_type, "message": message}


def _envelope(
    *,
    ok: bool,
    error: dict[str, str] | None,
    telemetry: dict[str, object] | None,
    result: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "ok": ok,
        "tool_name": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "request_schema_version": REQUEST_SCHEMA_VERSION,
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "error": error,
        "telemetry": telemetry,
        "result": result,
    }
