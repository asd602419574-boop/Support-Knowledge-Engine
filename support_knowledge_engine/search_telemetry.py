from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

# search_logs stays on the caller's connection. A second connection cannot
# insert while that connection still has an open write transaction, and the
# existing callers read the new row before they commit.
DEFAULT_TELEMETRY_DEADLINE_S = 1.0
TELEMETRY_RECORDED = "recorded"
TELEMETRY_FAILED = "failed"
TELEMETRY_TIMEOUT = "timeout"
_FAILED_WARNING = "telemetry 写入失败，检索结果仍然有效。"
_TIMEOUT_WARNING = "telemetry 超过时限，检索结果仍然有效。"

TelemetrySink = Callable[[sqlite3.Connection, Mapping[str, object]], None]


@dataclass(frozen=True)
class TelemetryReport:
    status: str
    warning: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {"status": self.status, "warning": self.warning}


def insert_search_log(connection: sqlite3.Connection, result: Mapping[str, object]) -> None:
    """Write the existing class-C search log. No page text or snippet is stored."""
    created_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    connection.execute(
        """INSERT INTO search_logs
           (original_query, normalized_query, applied_rules, recognized_products,
            match_state, result_count, elapsed_ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            result["original_query"],
            result["normalized_query"],
            json.dumps(list(result["applied_rules"]), ensure_ascii=False),
            json.dumps(list(result["recognized_products"]), ensure_ascii=False),
            result["match_state"],
            len(result["results"]),
            result["elapsed_ms"],
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
    """Best-effort telemetry after a result already exists. Never retrieves."""
    if sink is None:
        return _emit_inline(connection, result, deadline_s)
    return _emit_bounded_sink(connection, result, sink, deadline_s)


def _emit_inline(
    connection: sqlite3.Connection,
    result: Mapping[str, object],
    deadline_s: float,
) -> TelemetryReport:
    previous_timeout: int | None = None
    try:
        current = connection.execute("PRAGMA busy_timeout").fetchone()
        previous_timeout = int(current[0]) if current is not None else None
        deadline_ms = max(1, int(deadline_s * 1000))
        connection.execute(f"PRAGMA busy_timeout = {deadline_ms}")
        insert_search_log(connection, result)
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
    connection: sqlite3.Connection,
    result: Mapping[str, object],
    sink: TelemetrySink,
    deadline_s: float,
) -> TelemetryReport:
    outcome: dict[str, bool] = {}

    def _run_sink() -> None:
        try:
            sink(connection, result)
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
