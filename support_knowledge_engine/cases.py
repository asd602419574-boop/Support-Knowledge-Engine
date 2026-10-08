"""Case store separated from documents, pages, and search logs.

A case copies the G3 snapshot it was given. It does not read current pages
or document governance to rebuild that decision. Supporting text stays class
A. The decision-visible excerpt stays class B and is bound to class A by
evidence id and digests. Mutable context stores only a class-C marker.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .evidence import (
    DECISION_ABSTAIN,
    DECISION_CONFLICT,
    DECISION_SUPPORTED,
    PACKET_SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    EvidenceDecision,
    EvidencePacket,
    EvidenceSnapshot,
)
from .search_telemetry import class_c_query_text


CASE_CONTEXT_SCHEMA_VERSION = "1"
CLASS_A_POLICY = "class-a-evidence-source-v1"
CLASS_B_POLICY = "class-b-decision-visible-v1"
CLASS_A_POLICY_TEXT = (
    "Class A supporting original text is stored only on case_evidence for "
    "verification. Readers load it from the case store. It is not copied into "
    "search_logs, runtime trace payloads, or mutable case context. Retention "
    "follows the case until a governed retention action exists."
)
CLASS_B_POLICY_TEXT = (
    "Class B decision-visible text is stored on case_evidence and binds to "
    "the class A source through evidence_id, original_content_digest, and "
    "decision_visible_digest."
)
CLASS_C_CONTEXT_POLICY = (
    "Mutable case context stores a class-C marker for caller notes. It does "
    "not store raw request text that the trace redacted."
)
CASE_NOT_DURABLE = "case_not_durable"
CASE_INVALID = "case_invalid"
CASE_NOT_FOUND = "case_not_found"
CASE_NOT_DURABLE_MESSAGE = "case 不能在调用者未提交的事务中保证持久化。"
CASE_INVALID_MESSAGE = "case 拒绝写入不满足证据引用合同的内容。"
CASE_NOT_FOUND_MESSAGE = "case 不存在。"
_DECISIONS = frozenset({DECISION_SUPPORTED, DECISION_ABSTAIN, DECISION_CONFLICT})
_RETRIEVAL_STATES = frozenset(
    {
        "high_confidence",
        "possible_match",
        "ambiguous_product",
        "version_conflict",
        "outdated_only",
        "insufficient_evidence",
    }
)
_CASE_ID = re.compile(r"^case1-[0-9a-f]{32}$")
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_EVIDENCE_ID = re.compile(r"^ev1-[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PDF_SHA = re.compile(r"^[0-9a-f]{64}$")
_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
_MARKER = re.compile(r"^redacted:[0-9a-f]{12}:chars=\d+(?::omitted)?$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


class CaseStoreError(Exception):
    """The case store refused the operation. No partial case is committed."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class CaseEvidence:
    evidence_id: str
    snapshot_schema_version: str
    supporting_original_text: str
    document_lifecycle: str
    product_lifecycle: str | None
    authority_level: str
    original_content_digest: str
    decision_visible_representation: str
    decision_visible_digest: str
    pdf_sha256: str
    page_number: int
    document_identity: str
    captured_at: str
    source_policy: str
    visible_policy: str


@dataclass(frozen=True)
class CaseContext:
    schema_version: str
    note_marker: str | None


@dataclass(frozen=True)
class CaseRecord:
    case_id: str
    created_at: str
    decision_type: str
    reason_codes: tuple[str, ...]
    retrieval_state: str
    packet_schema_version: str
    context: CaseContext
    evidence: tuple[CaseEvidence, ...]
    trace_run_ids: tuple[str, ...]


def allocate_case_id() -> str:
    """Return one new case id. The id is not a place to hide request text."""

    return "case1-" + uuid.uuid4().hex


def create_case(
    connection: sqlite3.Connection,
    packet: EvidencePacket,
    decision: EvidenceDecision,
    *,
    case_id: str | None = None,
    run_id: str | None = None,
    note: str = "",
) -> CaseRecord:
    """Copy one in-memory decision into the case store and commit that copy."""

    _require_durable(connection)
    stored_case_id = allocate_case_id() if case_id is None else case_id
    if not isinstance(stored_case_id, str) or _CASE_ID.fullmatch(stored_case_id) is None:
        _invalid()
    evidence = _evidence_rows(packet, decision)
    reasons = _reason_codes(decision)
    if decision.decision_type not in _DECISIONS:
        _invalid()
    if decision.retrieval_state not in _RETRIEVAL_STATES:
        _invalid()
    if decision.packet_schema_version != PACKET_SCHEMA_VERSION:
        _invalid()
    if run_id is not None and (not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None):
        _invalid()
    context_json = _context_json(note)
    created_at = _now()
    try:
        connection.execute(
            """INSERT INTO support_cases (
                   case_id, created_at, context_json, context_schema_version,
                   context_updated_at, decision_type, reason_codes_json,
                   retrieval_state, packet_schema_version
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                stored_case_id,
                created_at,
                context_json,
                CASE_CONTEXT_SCHEMA_VERSION,
                created_at,
                decision.decision_type,
                json.dumps(list(reasons), ensure_ascii=False),
                decision.retrieval_state,
                decision.packet_schema_version,
            ),
        )
        for item in evidence:
            connection.execute(
                """INSERT INTO case_evidence (
                       case_id, evidence_id, snapshot_schema_version,
                       supporting_original_text, document_lifecycle,
                       product_lifecycle, authority_level, original_content_digest,
                       decision_visible_representation, decision_visible_digest,
                       pdf_sha256, page_number, document_identity, captured_at,
                       source_policy, visible_policy
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    stored_case_id,
                    item.evidence_id,
                    item.snapshot_schema_version,
                    item.supporting_original_text,
                    item.document_lifecycle,
                    item.product_lifecycle,
                    item.authority_level,
                    item.original_content_digest,
                    item.decision_visible_representation,
                    item.decision_visible_digest,
                    item.pdf_sha256,
                    item.page_number,
                    item.document_identity,
                    item.captured_at,
                    item.source_policy,
                    item.visible_policy,
                ),
            )
        if run_id is not None:
            connection.execute(
                """INSERT INTO case_trace_links (case_id, run_id, recorded_at)
                   VALUES (?, ?, ?)""",
                (stored_case_id, run_id, created_at),
            )
        connection.commit()
    except CaseStoreError:
        connection.rollback()
        raise
    except sqlite3.Error:
        connection.rollback()
        _invalid()
    return read_case(connection, stored_case_id)


def read_case(connection: sqlite3.Connection, case_id: str) -> CaseRecord:
    """Read one case from the case tables. Live documents are not consulted."""

    if not isinstance(case_id, str) or _CASE_ID.fullmatch(case_id) is None:
        _missing()
    row = connection.execute(
        """SELECT case_id, created_at, context_json, context_schema_version,
                  decision_type, reason_codes_json, retrieval_state,
                  packet_schema_version
           FROM support_cases WHERE case_id = ?""",
        (case_id,),
    ).fetchone()
    if row is None:
        _missing()
    evidence_rows = connection.execute(
        """SELECT evidence_id, snapshot_schema_version, supporting_original_text,
                  document_lifecycle, product_lifecycle, authority_level,
                  original_content_digest, decision_visible_representation,
                  decision_visible_digest, pdf_sha256, page_number,
                  document_identity, captured_at, source_policy, visible_policy
           FROM case_evidence WHERE case_id = ? ORDER BY id""",
        (case_id,),
    ).fetchall()
    links = connection.execute(
        """SELECT run_id FROM case_trace_links
           WHERE case_id = ? ORDER BY id""",
        (case_id,),
    ).fetchall()
    try:
        context = _parse_context(row["context_json"], row["context_schema_version"])
        reasons = _parse_reasons(row["reason_codes_json"])
        evidence = tuple(_row_evidence(item) for item in evidence_rows)
    except CaseStoreError:
        _invalid()
    return CaseRecord(
        case_id=str(row["case_id"]),
        created_at=str(row["created_at"]),
        decision_type=str(row["decision_type"]),
        reason_codes=reasons,
        retrieval_state=str(row["retrieval_state"]),
        packet_schema_version=str(row["packet_schema_version"]),
        context=context,
        evidence=evidence,
        trace_run_ids=tuple(str(item["run_id"]) for item in links),
    )


def write_case_context(
    connection: sqlite3.Connection,
    case_id: str,
    note: str,
) -> CaseRecord:
    """Replace the mutable class-C note. Evidence rows stay unchanged."""

    _require_durable(connection)
    if not isinstance(case_id, str) or _CASE_ID.fullmatch(case_id) is None:
        _missing()
    context_json = _context_json(note)
    exists = connection.execute(
        "SELECT 1 FROM support_cases WHERE case_id = ?",
        (case_id,),
    ).fetchone()
    if exists is None:
        _missing()
    if connection.in_transaction:
        _not_durable()
    try:
        connection.execute(
            """UPDATE support_cases
               SET context_json = ?, context_updated_at = ?
               WHERE case_id = ?""",
            (context_json, _now(), case_id),
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        _invalid()
    return read_case(connection, case_id)


def _evidence_rows(
    packet: EvidencePacket,
    decision: EvidenceDecision,
) -> tuple[CaseEvidence, ...]:
    if not isinstance(packet, EvidencePacket) or not isinstance(decision, EvidenceDecision):
        _invalid()
    if packet.packet_schema_version != PACKET_SCHEMA_VERSION:
        _invalid()
    if packet.retrieval_state != decision.retrieval_state:
        _invalid()
    by_id = {item.evidence_id: item for item in packet.evidence}
    if len(by_id) != len(packet.evidence):
        _invalid()
    chosen: list[CaseEvidence] = []
    seen: set[str] = set()
    for evidence_id in decision.evidence_ids:
        if not isinstance(evidence_id, str) or evidence_id in seen or evidence_id not in by_id:
            _invalid()
        seen.add(evidence_id)
        chosen.append(_copy_snapshot(by_id[evidence_id]))
    return tuple(chosen)


def _copy_snapshot(item: EvidenceSnapshot) -> CaseEvidence:
    if not isinstance(item, EvidenceSnapshot):
        _invalid()
    if item.snapshot_schema_version != SNAPSHOT_SCHEMA_VERSION:
        _invalid()
    if _EVIDENCE_ID.fullmatch(item.evidence_id) is None:
        _invalid()
    if _DIGEST.fullmatch(item.original_content_digest) is None:
        _invalid()
    if _DIGEST.fullmatch(item.decision_visible_digest) is None:
        _invalid()
    if _PDF_SHA.fullmatch(item.pdf_sha256) is None:
        _invalid()
    if item.document_identity != f"sha256:{item.pdf_sha256}":
        _invalid()
    if isinstance(item.page_number, bool) or not isinstance(item.page_number, int):
        _invalid()
    if item.page_number < 1:
        _invalid()
    if not isinstance(item.supporting_original_text, str):
        _invalid()
    if not isinstance(item.decision_visible_representation, str):
        _invalid()
    if not isinstance(item.document_lifecycle, str):
        _invalid()
    if not isinstance(item.authority_level, str):
        _invalid()
    if item.product_lifecycle is not None and not isinstance(item.product_lifecycle, str):
        _invalid()
    if _TIMESTAMP.fullmatch(item.captured_at) is None:
        _invalid()
    return CaseEvidence(
        evidence_id=item.evidence_id,
        snapshot_schema_version=item.snapshot_schema_version,
        supporting_original_text=item.supporting_original_text,
        document_lifecycle=item.document_lifecycle,
        product_lifecycle=item.product_lifecycle,
        authority_level=item.authority_level,
        original_content_digest=item.original_content_digest,
        decision_visible_representation=item.decision_visible_representation,
        decision_visible_digest=item.decision_visible_digest,
        pdf_sha256=item.pdf_sha256,
        page_number=item.page_number,
        document_identity=item.document_identity,
        captured_at=item.captured_at,
        source_policy=CLASS_A_POLICY,
        visible_policy=CLASS_B_POLICY,
    )


def _reason_codes(decision: EvidenceDecision) -> tuple[str, ...]:
    if not isinstance(decision.reason_codes, tuple):
        _invalid()
    cleaned: list[str] = []
    for code in decision.reason_codes:
        if not isinstance(code, str) or _REASON.fullmatch(code) is None:
            _invalid()
        cleaned.append(code)
    return tuple(cleaned)


def _context_json(note: object) -> str:
    if not isinstance(note, str):
        _invalid()
    if note == "":
        marker = None
    else:
        marker = class_c_query_text(note)
        if _MARKER.fullmatch(marker) is None or note in marker:
            _invalid()
    return json.dumps(
        {"note_marker": marker, "schema_version": CASE_CONTEXT_SCHEMA_VERSION},
        ensure_ascii=False,
        sort_keys=True,
    )


def _parse_context(payload: object, schema_version: object) -> CaseContext:
    if schema_version != CASE_CONTEXT_SCHEMA_VERSION or not isinstance(payload, str):
        _invalid()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        _invalid()
    if not isinstance(parsed, dict) or set(parsed) != {"note_marker", "schema_version"}:
        _invalid()
    if parsed["schema_version"] != CASE_CONTEXT_SCHEMA_VERSION:
        _invalid()
    marker = parsed["note_marker"]
    if marker is not None and (not isinstance(marker, str) or _MARKER.fullmatch(marker) is None):
        _invalid()
    return CaseContext(schema_version=CASE_CONTEXT_SCHEMA_VERSION, note_marker=marker)


def _parse_reasons(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, str):
        _invalid()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        _invalid()
    if not isinstance(parsed, list):
        _invalid()
    return _reason_codes(
        EvidenceDecision(
            decision_type=DECISION_ABSTAIN,
            reason_codes=tuple(parsed),
            retrieval_state="insufficient_evidence",
            evidence_ids=(),
            packet_schema_version=PACKET_SCHEMA_VERSION,
        )
    )


def _row_evidence(row: sqlite3.Row) -> CaseEvidence:
    page_number = row["page_number"]
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        _invalid()
    product_lifecycle = row["product_lifecycle"]
    if product_lifecycle is not None:
        product_lifecycle = str(product_lifecycle)
    item = CaseEvidence(
        evidence_id=str(row["evidence_id"]),
        snapshot_schema_version=str(row["snapshot_schema_version"]),
        supporting_original_text=str(row["supporting_original_text"]),
        document_lifecycle=str(row["document_lifecycle"]),
        product_lifecycle=product_lifecycle,
        authority_level=str(row["authority_level"]),
        original_content_digest=str(row["original_content_digest"]),
        decision_visible_representation=str(row["decision_visible_representation"]),
        decision_visible_digest=str(row["decision_visible_digest"]),
        pdf_sha256=str(row["pdf_sha256"]),
        page_number=page_number,
        document_identity=str(row["document_identity"]),
        captured_at=str(row["captured_at"]),
        source_policy=str(row["source_policy"]),
        visible_policy=str(row["visible_policy"]),
    )
    if item.source_policy != CLASS_A_POLICY or item.visible_policy != CLASS_B_POLICY:
        _invalid()
    if item.snapshot_schema_version != SNAPSHOT_SCHEMA_VERSION:
        _invalid()
    return item


def _require_durable(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        _not_durable()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _invalid() -> None:
    raise CaseStoreError(CASE_INVALID, CASE_INVALID_MESSAGE)


def _missing() -> None:
    raise CaseStoreError(CASE_NOT_FOUND, CASE_NOT_FOUND_MESSAGE)


def _not_durable() -> None:
    raise CaseStoreError(CASE_NOT_DURABLE, CASE_NOT_DURABLE_MESSAGE)
