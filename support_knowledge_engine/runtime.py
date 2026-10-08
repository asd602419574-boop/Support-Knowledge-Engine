"""Single-step deterministic runtime kernel.

One request runs one evidence capture. When capture succeeds, the kernel makes
one decision and stops. It does not reformulate a query, retrieve again, or
persist an execution record. The system remains a Retrieval System.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .evidence import (
    EvidenceCaptureError,
    EvidenceDecision,
    EvidencePacket,
    capture_evidence_packet,
    decide_evidence,
)


RUNTIME_VERSION = "1"
RUNTIME_REQUEST_SCHEMA_VERSION = "1"
RUNTIME_RESPONSE_SCHEMA_VERSION = "1"

ERROR_INVALID_REQUEST = "invalid_request"


class RuntimeCompositionError(Exception):
    """The packet and decision do not compose.

    This is a broken composition, not an evidence abstain and not a retrieval
    failure result.
    """


@dataclass(frozen=True)
class RuntimeRequest:
    """Immutable runtime request. Filters stay in the G2 and G3 contracts."""

    request_schema_version: str
    query: str
    product_id: str = ""
    product_series: str = ""
    document_type: str = ""
    status: str = ""
    association: str = ""
    firmware_version: str | None = None


@dataclass(frozen=True)
class RuntimeFailure:
    type: str
    message: str


@dataclass(frozen=True)
class RuntimeResult:
    ok: bool
    runtime_version: str
    runtime_response_schema_version: str
    request: RuntimeRequest | None
    packet: EvidencePacket | None
    decision: EvidenceDecision | None
    error: RuntimeFailure | None


def run_runtime(
    connection: sqlite3.Connection,
    request: object,
    *,
    retrieval_deadline_s: float | None = None,
) -> RuntimeResult:
    """Capture one packet and, when that succeeds, decide once.

    retrieval_deadline_s is an execution option. It is not part of the request.
    A capture error stays a structured failure. It does not become an abstain.
    """

    if not isinstance(request, RuntimeRequest):
        return _failure(None, ERROR_INVALID_REQUEST, "runtime request 必须是 RuntimeRequest。")
    if request.request_schema_version != RUNTIME_REQUEST_SCHEMA_VERSION:
        return _failure(request, ERROR_INVALID_REQUEST, "request_schema_version 不受支持。")

    try:
        packet = _capture(connection, request, retrieval_deadline_s)
    except EvidenceCaptureError as exc:
        return _failure(request, exc.error_type, str(exc))

    decision = decide_evidence(packet)
    _require_composed(packet, decision)
    return RuntimeResult(
        ok=True,
        runtime_version=RUNTIME_VERSION,
        runtime_response_schema_version=RUNTIME_RESPONSE_SCHEMA_VERSION,
        request=request,
        packet=packet,
        decision=decision,
        error=None,
    )


def _capture(
    connection: sqlite3.Connection,
    request: RuntimeRequest,
    retrieval_deadline_s: float | None,
) -> EvidencePacket:
    arguments: dict[str, object] = {
        "product_series": request.product_series,
        "document_type": request.document_type,
        "status": request.status,
        "association": request.association,
        "product_id": request.product_id,
        "firmware_version": request.firmware_version,
    }
    if retrieval_deadline_s is not None:
        arguments["retrieval_deadline_s"] = retrieval_deadline_s
    return capture_evidence_packet(connection, request.query, **arguments)


def _failure(
    request: RuntimeRequest | None,
    error_type: str,
    message: str,
) -> RuntimeResult:
    return RuntimeResult(
        ok=False,
        runtime_version=RUNTIME_VERSION,
        runtime_response_schema_version=RUNTIME_RESPONSE_SCHEMA_VERSION,
        request=request,
        packet=None,
        decision=None,
        error=RuntimeFailure(type=error_type, message=message),
    )


def _require_composed(packet: EvidencePacket, decision: EvidenceDecision) -> None:
    if decision.packet_schema_version != packet.packet_schema_version:
        raise RuntimeCompositionError("packet schema version 不一致。")
    known = {item.evidence_id for item in packet.evidence}
    if any(evidence_id not in known for evidence_id in decision.evidence_ids):
        raise RuntimeCompositionError("decision 引用了 packet 之外的 evidence。")
