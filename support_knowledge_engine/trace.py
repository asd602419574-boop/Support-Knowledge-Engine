"""Append-only class-C trace around one G4 runtime call.

The boundary calls run_runtime once. It does not retrieve again, read live
page text, or decide again. Sensitive text is rejected before INSERT.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NoReturn

from .evidence import EvidencePacket, EvidenceSnapshot
from .governance import DOCUMENT_STATUS_LABELS
from .runtime import RuntimeRequest, RuntimeResult, run_runtime
from .search_telemetry import class_c_query_text


TRACE_SCHEMA_VERSION = "1"
STEP_ID = "1"
REDACTION_REFUSAL = "redaction_refusal"
TRACE_WRITE_FAILURE = "trace_write_failure"
TRACE_NOT_DURABLE = "trace_not_durable"
REDACTION_REFUSAL_MESSAGE = "runtime trace 拒绝写入未脱敏内容。"
TRACE_WRITE_FAILURE_MESSAGE = "runtime trace 写入失败。"
TRACE_NOT_DURABLE_MESSAGE = "runtime trace 不能在调用者未提交的事务中保证持久化。"
_TRUSTED_STATUS = frozenset(DOCUMENT_STATUS_LABELS)
_TRUSTED_ASSOCIATION = frozenset({"linked", "unlinked"})

_DECISIONS = frozenset({"supported", "abstain", "conflict"})
_FAILURES = frozenset(
    {
        "invalid_request",
        "retrieval_failure",
        "retrieval_timeout",
        "source_index_mismatch",
    }
)
_TERMINATIONS = _DECISIONS | _FAILURES
_FORBIDDEN_KEYS = frozenset(
    {
        "supporting_original_text",
        "decision_visible_representation",
        "original_query",
        "normalized_query",
        "retrieval_query",
        "snippet",
        "answer_text",
        "query_text",
        "content",
        "filename",
        "source_url",
        "source_locator",
        "firmware_range",
        "canonical_product_name",
    }
)
_MARKER_ONLY_KEYS = frozenset({"query", "firmware_version", "message"})
_MARKER = re.compile(r"^redacted:[0-9a-f]{12}:chars=\d+(?::omitted)?$")
_EVIDENCE_ID = re.compile(r"^ev1-[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENUM = re.compile(r"^[a-z0-9_]{1,64}$")
_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\w)\+?\d{1,3}[-.\s](?:\d{2,4}[-.\s]){2,}\d{2,4}(?!\w)")
_SERIAL = re.compile(r"SN-[A-Za-z0-9]{6,}", re.IGNORECASE)
_INSERT = """INSERT INTO runtime_traces (
    run_id, step_id, tool_name, tool_version, runtime_version,
    request_schema_version, response_schema_version,
    runtime_request_schema_version, runtime_response_schema_version,
    input_json, output_json, decision_json, evidence_ids,
    latency_ms, termination_reason, created_at, trace_schema_version
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


class TracePersistenceError(Exception):
    """The trace was not written. The runtime result stays unchanged."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class TraceFailure:
    type: str
    message: str


@dataclass(frozen=True)
class TraceExecution:
    runtime: RuntimeResult
    trace_ok: bool
    run_id: str | None
    error: TraceFailure | None


@dataclass(frozen=True)
class TraceRecord:
    run_id: str
    step_id: str
    tool_name: str | None
    tool_version: str | None
    runtime_version: str
    request_schema_version: str | None
    response_schema_version: str | None
    runtime_request_schema_version: str | None
    runtime_response_schema_version: str
    input_payload: dict[str, object]
    output_payload: dict[str, object]
    decision_payload: dict[str, object]
    evidence_ids: tuple[str, ...]
    latency_ms: float
    termination_reason: str
    created_at: str
    trace_schema_version: str


def execute_traced_runtime(
    connection: sqlite3.Connection,
    request: object,
    *,
    retrieval_deadline_s: float | None = None,
) -> TraceExecution:
    """Run one runtime call, then append one redacted trace of that result."""

    started = time.perf_counter()
    arguments: dict[str, float] = {}
    if retrieval_deadline_s is not None:
        arguments["retrieval_deadline_s"] = retrieval_deadline_s
    result = run_runtime(connection, request, **arguments)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    try:
        record = project_class_c_record(result, elapsed_ms)
        stored_run_id = commit_class_c_trace(connection, record)
    except TracePersistenceError as exc:
        return TraceExecution(
            runtime=result,
            trace_ok=False,
            run_id=None,
            error=TraceFailure(exc.error_type, str(exc)),
        )
    except sqlite3.Error:
        return TraceExecution(
            runtime=result,
            trace_ok=False,
            run_id=None,
            error=TraceFailure(TRACE_WRITE_FAILURE, TRACE_WRITE_FAILURE_MESSAGE),
        )
    return TraceExecution(
        runtime=result,
        trace_ok=True,
        run_id=stored_run_id,
        error=None,
    )


def project_class_c_record(
    result: RuntimeResult,
    latency_ms: float,
    *,
    run_id: str | None = None,
    created_at: str | None = None,
) -> TraceRecord:
    """Project one in-memory runtime result into a class-C record.

    Tool columns come from the packet. A failure that has no packet keeps
    those columns empty instead of inventing a tool call.
    """

    packet = result.packet
    decision = result.decision
    error = result.error
    if (packet is None) != (decision is None) or (decision is None) == (error is None):
        _refuse()
    if decision is not None:
        if packet is None:
            _refuse()
        termination = decision.decision_type
        if termination not in _DECISIONS:
            _refuse()
        evidence_ids = tuple(decision.evidence_ids)
        known = {item.evidence_id for item in packet.evidence}
        if any(not isinstance(item, str) or item not in known for item in evidence_ids):
            _refuse()
    elif error is not None:
        termination = error.type
        if termination not in _FAILURES:
            _refuse()
        evidence_ids = ()
    else:
        _refuse()

    if packet is None:
        tool_name = None
        tool_version = None
        request_schema_version = None
        response_schema_version = None
        output_payload: dict[str, object] = {
            "retrieval_state": None,
            "packet_schema_version": None,
            "evidence": [],
        }
    else:
        tool_name = packet.retrieval_tool_name
        tool_version = packet.retrieval_tool_version
        request_schema_version = packet.request.request_schema_version
        response_schema_version = packet.retrieval_response_schema_version
        output_payload = {
            "retrieval_state": packet.retrieval_state,
            "packet_schema_version": packet.packet_schema_version,
            "evidence": [_evidence_summary(item) for item in packet.evidence],
        }

    if decision is None:
        if error is None or not isinstance(error.message, str) or not isinstance(error.type, str):
            _refuse()
        decision_payload = {
            "decision_type": None,
            "reason_codes": [],
            "evidence_ids": [],
            "packet_schema_version": None,
            "retrieval_state": None,
            "error": {"type": error.type, "message": class_c_query_text(error.message)},
        }
    else:
        decision_payload = {
            "decision_type": decision.decision_type,
            "reason_codes": list(decision.reason_codes),
            "evidence_ids": list(evidence_ids),
            "packet_schema_version": decision.packet_schema_version,
            "retrieval_state": decision.retrieval_state,
        }

    record = TraceRecord(
        run_id=uuid.uuid4().hex if run_id is None else run_id,
        step_id=STEP_ID,
        tool_name=tool_name,
        tool_version=tool_version,
        runtime_version=result.runtime_version,
        request_schema_version=request_schema_version,
        response_schema_version=response_schema_version,
        runtime_request_schema_version=(
            result.request.request_schema_version
            if isinstance(result.request, RuntimeRequest)
            else None
        ),
        runtime_response_schema_version=result.runtime_response_schema_version,
        input_payload=_input_payload(result.request),
        output_payload=output_payload,
        decision_payload=decision_payload,
        evidence_ids=evidence_ids,
        latency_ms=latency_ms,
        termination_reason=termination,
        created_at=(
            datetime.now(timezone.utc).isoformat(timespec="seconds")
            if created_at is None
            else created_at
        ),
        trace_schema_version=TRACE_SCHEMA_VERSION,
    )
    _assert_record_shape(record)
    _assert_no_source_leak(result, record)
    return record


def commit_class_c_trace(connection: sqlite3.Connection, record: TraceRecord) -> str:
    """Insert one projected record and commit that insert.

    A caller-owned transaction is refused before INSERT. This function does
    not commit the caller's transaction.
    """

    _assert_record_shape(record)
    columns = _json_columns(record)
    if _contains_sensitive_pattern(_stored_text(record, columns)):
        _refuse()
    if connection.in_transaction:
        raise TracePersistenceError(TRACE_NOT_DURABLE, TRACE_NOT_DURABLE_MESSAGE)
    try:
        connection.execute(
            _INSERT,
            (
                record.run_id,
                record.step_id,
                record.tool_name,
                record.tool_version,
                record.runtime_version,
                record.request_schema_version,
                record.response_schema_version,
                record.runtime_request_schema_version,
                record.runtime_response_schema_version,
                columns["input_json"],
                columns["output_json"],
                columns["decision_json"],
                columns["evidence_ids"],
                record.latency_ms,
                record.termination_reason,
                record.created_at,
                record.trace_schema_version,
            ),
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    return record.run_id


def _input_payload(request: object) -> dict[str, object]:
    if not isinstance(request, RuntimeRequest):
        return {
            "request_schema_version": None,
            "query": None,
            "product_id": None,
            "product_series": None,
            "document_type": None,
            "status": None,
            "association": None,
            "firmware_version": None,
        }
    firmware = request.firmware_version
    if firmware is None:
        stored_firmware: str | None = None
    elif isinstance(firmware, str):
        stored_firmware = class_c_query_text(firmware)
    else:
        stored_firmware = None
    return {
        "request_schema_version": request.request_schema_version,
        "query": _class_c_query(request.query),
        "product_id": _class_c_filter(request.product_id, field="product_id"),
        "product_series": _class_c_filter(request.product_series, field="product_series"),
        "document_type": _class_c_filter(request.document_type, field="document_type"),
        "status": _class_c_filter(request.status, field="status"),
        "association": _class_c_filter(request.association, field="association"),
        "firmware_version": stored_firmware,
    }


def _class_c_query(value: object) -> str:
    if not isinstance(value, str):
        _refuse()
    return class_c_query_text(value)


def _class_c_filter(value: object, *, field: str) -> str:
    """Store only an explicit allowlist. Free text is always a class-C marker."""

    if not isinstance(value, str):
        _refuse()
    if value == "":
        return value
    if _contains_sensitive_pattern(value):
        return class_c_query_text(value)
    if field == "product_id" and value.isdigit():
        return value
    if field == "status" and value in _TRUSTED_STATUS:
        return value
    if field == "association" and value in _TRUSTED_ASSOCIATION:
        return value
    return class_c_query_text(value)


def _evidence_summary(item: EvidenceSnapshot) -> dict[str, object]:
    return {
        "evidence_id": item.evidence_id,
        "page_number": item.page_number,
        "document_identity": item.document_identity,
        "document_lifecycle": item.document_lifecycle,
        "product_lifecycle": item.product_lifecycle,
        "firmware_applicability": item.firmware_applicability,
        "authority_level": item.authority_level,
        "snapshot_schema_version": item.snapshot_schema_version,
        "canonical_product_id": item.canonical_product_id,
    }


def _assert_no_source_leak(result: RuntimeResult, record: TraceRecord) -> None:
    blob = _stored_text(record, _json_columns(record))
    for secret in _sensitive_sources(result):
        if secret in blob:
            _refuse()
    if _contains_sensitive_pattern(blob):
        _refuse()


def _sensitive_sources(result: RuntimeResult) -> set[str]:
    found: set[str] = set()
    request = result.request
    if isinstance(request, RuntimeRequest):
        for value in (
            request.query,
            request.firmware_version,
            request.product_id,
            request.product_series,
            request.document_type,
            request.status,
            request.association,
        ):
            _remember(found, value)
    if result.error is not None:
        _remember(found, result.error.message)
    packet = result.packet
    if isinstance(packet, EvidencePacket):
        for value in (
            packet.request.original_query,
            packet.request.normalized_query,
            packet.request.retrieval_query,
            packet.request.firmware_version,
            packet.request.explicit_product_name,
            packet.request.product_series,
            packet.request.document_type,
            packet.request.status,
            packet.request.association,
        ):
            _remember(found, value)
        for product in packet.recognized_products:
            _remember(found, product.name)
        for item in packet.evidence:
            for value in (
                item.supporting_original_text,
                item.decision_visible_representation,
                item.filename,
                item.source_url,
                item.source_locator,
                item.firmware_range,
                item.canonical_product_name,
            ):
                _remember(found, value)
    return found


def _remember(found: set[str], value: object) -> None:
    if not isinstance(value, str) or value == "":
        return
    if len(value) >= 12 or _contains_sensitive_pattern(value):
        found.add(value)


def _json_columns(record: TraceRecord) -> dict[str, str]:
    return {
        "input_json": _dump(record.input_payload),
        "output_json": _dump(record.output_payload),
        "decision_json": _dump(record.decision_payload),
        "evidence_ids": _dump(list(record.evidence_ids)),
    }


def _stored_text(record: TraceRecord, columns: dict[str, str]) -> str:
    scalar = (
        record.run_id,
        record.step_id,
        record.tool_name,
        record.tool_version,
        record.runtime_version,
        record.request_schema_version,
        record.response_schema_version,
        record.runtime_request_schema_version,
        record.runtime_response_schema_version,
        record.termination_reason,
        record.created_at,
        record.trace_schema_version,
    )
    return "\n".join(part for part in (*scalar, *columns.values()) if isinstance(part, str))


def _dump(value: object) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        _refuse()
    if not isinstance(rendered, str):
        _refuse()
    return rendered


def _assert_record_shape(record: TraceRecord) -> None:
    if not isinstance(record, TraceRecord):
        _refuse()
    if not re.fullmatch(r"[0-9a-f]{32}", record.run_id):
        _refuse()
    if record.step_id != STEP_ID or record.trace_schema_version != TRACE_SCHEMA_VERSION:
        _refuse()
    if record.termination_reason not in _TERMINATIONS:
        _refuse()
    if isinstance(record.latency_ms, bool) or not isinstance(record.latency_ms, (int, float)):
        _refuse()
    if not math.isfinite(float(record.latency_ms)) or float(record.latency_ms) < 0:
        _refuse()
    if not _TIMESTAMP.fullmatch(record.created_at):
        _refuse()
    for name in (
        "tool_name",
        "tool_version",
        "runtime_version",
        "request_schema_version",
        "response_schema_version",
        "runtime_request_schema_version",
        "runtime_response_schema_version",
    ):
        value = getattr(record, name)
        if value is not None and not _safe_token(value):
            _refuse()
    if not _safe_token(record.runtime_version) or not _safe_token(record.runtime_response_schema_version):
        _refuse()
    _walk(record.input_payload)
    _walk(record.output_payload)
    _walk(record.decision_payload)
    if not isinstance(record.evidence_ids, tuple):
        _refuse()
    for item in record.evidence_ids:
        if not isinstance(item, str) or not _EVIDENCE_ID.fullmatch(item):
            _refuse()
    decision_ids = record.decision_payload.get("evidence_ids")
    if not isinstance(decision_ids, list) or list(record.evidence_ids) != decision_ids:
        _refuse()


def _walk(value: object, *, key: str | None = None) -> None:
    if key in _FORBIDDEN_KEYS:
        _refuse()
    if key in _MARKER_ONLY_KEYS:
        if value is None:
            return
        if isinstance(value, str) and _MARKER.fullmatch(value):
            return
        _refuse()
    if key == "page_number":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            _refuse()
        return
    if key == "canonical_product_id":
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            _refuse()
        return
    if value is None:
        return
    if isinstance(value, bool) or isinstance(value, (bytes, bytearray)):
        _refuse()
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _refuse()
        return
    if isinstance(value, str):
        if _allowed_string(value):
            return
        _refuse()
    if isinstance(value, list):
        for item in value:
            _walk(item)
        return
    if isinstance(value, dict):
        for child_key, child in value.items():
            if not isinstance(child_key, str) or not _KEY.fullmatch(child_key):
                _refuse()
            _walk(child, key=child_key)
        return
    _refuse()


def _allowed_string(value: str) -> bool:
    if _contains_sensitive_pattern(value):
        return False
    if value == "":
        return True
    return bool(
        _MARKER.fullmatch(value)
        or _EVIDENCE_ID.fullmatch(value)
        or _DIGEST.fullmatch(value)
        or _ENUM.fullmatch(value)
        or _VERSION.fullmatch(value)
        or _TIMESTAMP.fullmatch(value)
    )


def _safe_token(value: object) -> bool:
    return isinstance(value, str) and bool(_ENUM.fullmatch(value) or _VERSION.fullmatch(value)) and not _contains_sensitive_pattern(value)


def _contains_sensitive_pattern(value: str) -> bool:
    return bool(_EMAIL.search(value) or _PHONE.search(value) or _SERIAL.search(value))


def _refuse() -> NoReturn:
    raise TracePersistenceError(REDACTION_REFUSAL, REDACTION_REFUSAL_MESSAGE)
