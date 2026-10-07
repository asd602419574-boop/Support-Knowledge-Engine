from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

# The default writer uses the caller's connection on the caller thread. A second
# connection cannot insert while that transaction is still open, and existing
# callers read the new row before they commit.
DEFAULT_TELEMETRY_DEADLINE_S = 1.0
TELEMETRY_RECORDED = "recorded"
TELEMETRY_FAILED = "failed"
TELEMETRY_TIMEOUT = "timeout"
_FAILED_WARNING = "telemetry 写入失败，检索结果仍然有效。"
_TIMEOUT_WARNING = "telemetry 超过时限，检索结果仍然有效。"


@dataclass(frozen=True)
class TelemetryPayload:
    """Immutable class-C record. It has no raw query and no SQLite connection."""

    query_representation: str
    normalized_representation: str
    applied_rules: tuple[str, ...]
    recognized_products: tuple[tuple[int, str], ...]
    match_state: str
    result_count: int
    elapsed_ms: float


TelemetrySink = Callable[[TelemetryPayload], None]


@dataclass(frozen=True)
class TelemetryReport:
    status: str
    warning: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {"status": self.status, "warning": self.warning}


def class_c_query_text(value: str) -> str:
    """Non-reversible class-C marker. Normalization is not treated as redaction."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    rendered = f"redacted:{digest}:chars={len(value)}"
    if value and value in rendered:
        return f"{rendered}:omitted"
    return rendered


def build_telemetry_payload(result: Mapping[str, object]) -> TelemetryPayload:
    products = tuple(
        (int(item["id"]), str(item["name"]))
        for item in result["recognized_products"]
    )
    return TelemetryPayload(
        query_representation=class_c_query_text(str(result["original_query"])),
        normalized_representation=class_c_query_text(str(result["normalized_query"])),
        applied_rules=tuple(str(item) for item in result["applied_rules"]),
        recognized_products=products,
        match_state=str(result["match_state"]),
        result_count=len(result["results"]),
        elapsed_ms=float(result["elapsed_ms"]),
    )


def insert_search_log(connection: sqlite3.Connection, payload: TelemetryPayload) -> None:
    """Write class-C columns only. The two query columns store representations."""
    created_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    connection.execute(
        """INSERT INTO search_logs
           (original_query, normalized_query, applied_rules, recognized_products,
            match_state, result_count, elapsed_ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload.query_representation,
            payload.normalized_representation,
            json.dumps(list(payload.applied_rules), ensure_ascii=False),
            json.dumps(
                [{"id": product_id, "name": name} for product_id, name in payload.recognized_products],
                ensure_ascii=False,
            ),
            payload.match_state,
            payload.result_count,
            payload.elapsed_ms,
            created_at,
        ),
    )


def emit_search_telemetry(
    connection: sqlite3.Connection,
    result: Mapping[str, object],
    *,
    sink: TelemetrySink | None = None,
    deadline_s: float = DEFAULT_TELEMETRY_DEADLINE_S,
) -> TelemetryReport:
    """Best-effort telemetry after a result exists. Never retrieves.

    The default writer runs on the caller thread and uses that connection.
    A custom sink receives only an immutable payload and runs on a daemon
    worker. After a timeout the response is already final. The worker is not
    cancelled and may finish later; it cannot see the caller connection or
    change the response.
    """
    payload = build_telemetry_payload(result)
    if sink is None:
        return _emit_inline(connection, payload, deadline_s)
    return _emit_bounded_sink(payload, sink, deadline_s)


def _emit_inline(
    connection: sqlite3.Connection,
    payload: TelemetryPayload,
    deadline_s: float,
) -> TelemetryReport:
    previous_timeout: int | None = None
    try:
        current = connection.execute("PRAGMA busy_timeout").fetchone()
        previous_timeout = int(current[0]) if current is not None else None
        deadline_ms = max(1, int(deadline_s * 1000))
        connection.execute(f"PRAGMA busy_timeout = {deadline_ms}")
        insert_search_log(connection, payload)
        return TelemetryReport(TELEMETRY_RECORDED)
    except Exception:
        return TelemetryReport(TELEMETRY_FAILED, _FAILED_WARNING)
    finally:
        if previous_timeout is not None:
            try:
                connection.execute(f"PRAGMA busy_timeout = {previous_timeout}")
            except sqlite3.Error:
                pass


def _emit_bounded_sink(
    payload: TelemetryPayload,
    sink: TelemetrySink,
    deadline_s: float,
) -> TelemetryReport:
    outcome: dict[str, bool] = {}

    def _run_sink() -> None:
        try:
            sink(payload)
        except Exception:
            outcome["failed"] = True
        else:
            outcome["recorded"] = True

    worker = threading.Thread(target=_run_sink, name="search-telemetry", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline_s))
    if worker.is_alive():
        return TelemetryReport(TELEMETRY_TIMEOUT, _TIMEOUT_WARNING)
    if outcome.get("recorded"):
        return TelemetryReport(TELEMETRY_RECORDED)
    return TelemetryReport(TELEMETRY_FAILED, _FAILED_WARNING)
