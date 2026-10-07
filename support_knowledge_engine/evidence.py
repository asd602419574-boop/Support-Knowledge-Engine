"""Immutable evidence packet and evidence decision.

One capture executes the G2 retrieval tool once inside a single read
transaction, then reads source page text in that same snapshot. The page text
and the full-text index row must be the same content. The packet stores the
tool response's provenance, that page text, and the snippet the tool actually
returned. The evidence id binds both the page and that snippet. decide_evidence
reads only the packet.

An empty firmware_range is applicable. Any non-empty value is unknown: this
corpus has no supported firmware grammar, so a dotted version is not treated
as an exact match or mismatch.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from .governance import AUTHORITY_LEVEL_LABELS, normalize_alias
from .retrieval_tool import REQUEST_SCHEMA_VERSION, execute_retrieval_tool


SNAPSHOT_SCHEMA_VERSION = "3"
PACKET_SCHEMA_VERSION = "2"
TRANSFORMATION_VERSION = "retrieval-excerpt-v1"
SUPPORTING_TEXT_SOURCE = "page-content"
DECISION_VISIBLE_SOURCE = "G2 retrieval snippet"

DECISION_SUPPORTED = "supported"
DECISION_ABSTAIN = "abstain"
DECISION_CONFLICT = "conflict"

REASON_AMBIGUOUS_PRODUCT = "ambiguous_product"
REASON_AMBIGUOUS_PRODUCT_ALIAS = "ambiguous_product_alias"
REASON_VERSION_CONFLICT = "version_conflict"
REASON_PRODUCT_FILTER_CONFLICT = "product_filter_conflict"
REASON_EVIDENCE_PRODUCT_MISMATCH = "evidence_product_mismatch"
REASON_INACTIVE_PRODUCT = "inactive_product"
REASON_ARCHIVED_PRODUCT = "archived_product"
REASON_PLANNED_PRODUCT = "planned_product"
REASON_PRODUCT_LIFECYCLE_UNKNOWN = "product_lifecycle_unknown"
REASON_OUTDATED_DOCUMENT = "outdated_document"
REASON_DOCUMENT_NEEDS_REVIEW = "document_needs_review"
REASON_DOCUMENT_DRAFT = "document_draft"
REASON_DOCUMENT_LIFECYCLE_UNKNOWN = "document_lifecycle_unknown"
REASON_FIRMWARE_NOT_APPLICABLE = "firmware_not_applicable"
REASON_FIRMWARE_UNKNOWN = "firmware_unknown"
REASON_AUTHORITY_UNKNOWN = "authority_unknown"
REASON_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
REASON_POSSIBLE_MATCH = "possible_match"

KNOWN_AUTHORITY_LEVELS = frozenset(AUTHORITY_LEVEL_LABELS)
_PRODUCT_LIFECYCLE_REASONS = {
    "inactive": REASON_INACTIVE_PRODUCT,
    "archived": REASON_ARCHIVED_PRODUCT,
    "planned": REASON_PLANNED_PRODUCT,
}
_DOCUMENT_LIFECYCLE_REASONS = {
    "superseded": REASON_OUTDATED_DOCUMENT,
    "archived": REASON_OUTDATED_DOCUMENT,
    "needs_review": REASON_DOCUMENT_NEEDS_REVIEW,
    "draft": REASON_DOCUMENT_DRAFT,
}
_RESULT_TEXT_FIELDS = ("original_query", "normalized_query", "retrieval_query", "match_state")
_ENVELOPE_TEXT_FIELDS = (
    "tool_name",
    "tool_version",
    "request_schema_version",
    "response_schema_version",
)


class EvidenceCaptureError(Exception):
    """The G2 tool did not return a usable result. No packet is produced."""

    def __init__(self, error_type: str, message: str) -> None:
        self.error_type = error_type
        super().__init__(message)


@dataclass(frozen=True)
class RecognizedProduct:
    product_id: int
    name: str
    lifecycle: str | None


@dataclass(frozen=True)
class RequestContext:
    """Request frozen from the G2 call. Queries come from that tool result."""

    original_query: str
    normalized_query: str
    retrieval_query: str
    request_schema_version: str
    explicit_product_id: int | None
    explicit_product_name: str | None
    explicit_product_lifecycle: str | None
    firmware_version: str | None
    product_series: str
    document_type: str
    status: str
    association: str


@dataclass(frozen=True)
class EvidenceSnapshot:
    evidence_id: str
    snapshot_schema_version: str
    document_id: int
    document_identity: str
    filename: str
    pdf_sha256: str
    page_number: int
    source_locator: str
    source_url: str
    supporting_original_text: str
    supporting_text_source: str
    original_content_digest: str
    metadata_digest: str
    canonical_product_id: int | None
    canonical_product_name: str | None
    product_lifecycle: str | None
    document_lifecycle: str
    firmware_range: str
    firmware_applicability: str
    authority_level: str
    retrieval_tool_name: str
    retrieval_tool_version: str
    retrieval_response_schema_version: str
    captured_at: str
    decision_visible_representation: str
    decision_visible_digest: str
    decision_visible_source: str
    transformation_version: str


@dataclass(frozen=True)
class EvidencePacket:
    packet_schema_version: str
    retrieval_state: str
    recognized_products: tuple[RecognizedProduct, ...]
    request: RequestContext
    alias_conflict: bool
    evidence: tuple[EvidenceSnapshot, ...]
    captured_at: str
    retrieval_tool_name: str
    retrieval_tool_version: str
    retrieval_response_schema_version: str


@dataclass(frozen=True)
class EvidenceDecision:
    decision_type: str
    reason_codes: tuple[str, ...]
    retrieval_state: str
    evidence_ids: tuple[str, ...]
    packet_schema_version: str


@dataclass(frozen=True)
class _ToolProvenance:
    tool_name: str
    tool_version: str
    request_schema_version: str
    response_schema_version: str


def classify_firmware(firmware_range: str, firmware_version: str | None) -> str:
    """Return applicable or unknown.

    An empty stored range has no restriction. Every non-empty value stays
    unknown. firmware_version cannot turn that value into a match or mismatch
    until a corpus-backed grammar exists.
    """

    del firmware_version
    if firmware_range.strip() == "":
        return "applicable"
    return "unknown"


def decide_evidence(packet: EvidencePacket) -> EvidenceDecision:
    """Decide from the packet only. This function does not take a connection."""

    reasons = _packet_reasons(packet)
    blocking = [(item, _evidence_reasons(packet, item)) for item in packet.evidence]
    clean_ids = tuple(item.evidence_id for item, codes in blocking if not codes)
    all_ids = tuple(item.evidence_id for item in packet.evidence)

    if packet.retrieval_state == "version_conflict":
        reasons.extend(code for _, codes in blocking for code in codes)
        decision_type = DECISION_CONFLICT
        evidence_ids = all_ids
    elif packet.retrieval_state == "high_confidence" and not reasons and clean_ids:
        decision_type = DECISION_SUPPORTED
        evidence_ids = clean_ids
        reasons = []
    else:
        reasons.extend(code for _, codes in blocking for code in codes)
        decision_type = DECISION_ABSTAIN
        evidence_ids = all_ids

    return EvidenceDecision(
        decision_type=decision_type,
        reason_codes=tuple(sorted(set(reasons))),
        retrieval_state=packet.retrieval_state,
        evidence_ids=evidence_ids,
        packet_schema_version=packet.packet_schema_version,
    )


def capture_evidence_packet(
    connection: sqlite3.Connection,
    query: str,
    product_series: str = "",
    document_type: str = "",
    status: str = "",
    association: str = "",
    product_id: str = "",
    firmware_version: str | None = None,
    captured_at: str | None = None,
    retrieval_deadline_s: float | None = None,
) -> EvidencePacket:
    """Run one G2 tool call and freeze the packet before the read transaction ends.

    The tool response supplies retrieval state, the snippet, and tool provenance.
    Page text, product lifecycle, and alias conflict are read in that same
    transaction. A transaction this function opened is rolled back. A caller
    transaction is left alone. No search_logs write and no second retrieval.
    """

    timestamp = captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    tool_request = _tool_request(
        query, product_series, document_type, status, association, product_id
    )
    with _read_boundary(connection):
        if retrieval_deadline_s is None:
            envelope = execute_retrieval_tool(
                connection, tool_request, telemetry_sink=_noop_telemetry
            )
        else:
            envelope = execute_retrieval_tool(
                connection,
                tool_request,
                telemetry_sink=_noop_telemetry,
                retrieval_deadline_s=retrieval_deadline_s,
            )
        result = _accepted_result(envelope)
        provenance = _provenance(envelope)
        explicit_product_id = int(product_id) if str(product_id).isdigit() else None
        page_contents, products, alias_conflict = _load_snapshot_facts(
            connection, result, explicit_product_id
        )
        return _assemble_packet(
            result,
            page_contents,
            products,
            alias_conflict,
            provenance,
            RequestContext(
                original_query=str(result["original_query"]),
                normalized_query=str(result["normalized_query"]),
                retrieval_query=str(result["retrieval_query"]),
                request_schema_version=provenance.request_schema_version,
                explicit_product_id=explicit_product_id,
                explicit_product_name=_product_name(products, explicit_product_id),
                explicit_product_lifecycle=_product_lifecycle(products, explicit_product_id),
                firmware_version=None if firmware_version is None else firmware_version.strip(),
                product_series=str(tool_request["product_series"]),
                document_type=str(tool_request["document_type"]),
                status=str(tool_request["status"]),
                association=str(tool_request["association"]),
            ),
            timestamp,
        )


@contextmanager
def _read_boundary(connection: sqlite3.Connection) -> Iterator[None]:
    owns_read = not connection.in_transaction
    if owns_read:
        connection.execute("BEGIN DEFERRED")
    try:
        yield
    finally:
        if owns_read:
            connection.rollback()


def _noop_telemetry(_payload: object) -> None:
    """Class-C sink that keeps the capture transaction read-only."""


def _tool_request(
    query: str,
    product_series: str,
    document_type: str,
    status: str,
    association: str,
    product_id: str,
) -> dict[str, object]:
    return {
        "request_schema_version": REQUEST_SCHEMA_VERSION,
        "query": query,
        "product_id": product_id,
        "product_series": product_series,
        "document_type": document_type,
        "status": status,
        "association": association,
    }


def _accepted_result(envelope: object) -> Mapping[str, object]:
    if not isinstance(envelope, Mapping):
        raise EvidenceCaptureError("retrieval_failure", "检索响应无效。")
    if not envelope.get("ok"):
        error = envelope.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("type"), str):
            message = error.get("message")
            text = message if isinstance(message, str) and message else "检索失败。"
            raise EvidenceCaptureError(str(error["type"]), text)
        raise EvidenceCaptureError("retrieval_failure", "检索失败。")
    result = envelope.get("result")
    if not isinstance(result, Mapping):
        raise EvidenceCaptureError("retrieval_failure", "检索响应缺少结果。")
    for key in _RESULT_TEXT_FIELDS:
        if not isinstance(result.get(key), str):
            raise EvidenceCaptureError("retrieval_failure", "检索响应缺少请求出处。")
    if not isinstance(result.get("recognized_products"), list):
        raise EvidenceCaptureError("retrieval_failure", "检索响应缺少结果。")
    if not isinstance(result.get("results"), list):
        raise EvidenceCaptureError("retrieval_failure", "检索响应缺少结果。")
    return result


def _provenance(envelope: Mapping[str, object]) -> _ToolProvenance:
    values: dict[str, str] = {}
    for key in _ENVELOPE_TEXT_FIELDS:
        value = envelope.get(key)
        if not isinstance(value, str) or not value:
            raise EvidenceCaptureError("retrieval_failure", "检索响应缺少工具出处。")
        values[key] = value
    return _ToolProvenance(
        tool_name=values["tool_name"],
        tool_version=values["tool_version"],
        request_schema_version=values["request_schema_version"],
        response_schema_version=values["response_schema_version"],
    )


def _load_snapshot_facts(
    connection: sqlite3.Connection,
    result: Mapping[str, object],
    explicit_product_id: int | None,
) -> tuple[dict[tuple[int, int], str], dict[int, tuple[str, str]], bool]:
    """Read source text and product facts in the open capture transaction.

    The tool response is already in memory. These selects are not a retrieval.
    """

    page_contents = _load_page_contents(connection, result["results"])
    products = _load_products(connection, _collect_product_ids(result, explicit_product_id))
    recognized = tuple(int(item["id"]) for item in result["recognized_products"])
    alias_conflict = _load_alias_conflict(
        connection, str(result["normalized_query"]), recognized
    )
    return page_contents, products, alias_conflict


def _load_page_contents(
    connection: sqlite3.Connection, rows: object
) -> dict[tuple[int, int], str]:
    if not isinstance(rows, list):
        raise EvidenceCaptureError("retrieval_failure", "检索响应缺少结果。")
    contents: dict[tuple[int, int], str] = {}
    for row in rows:
        document_id = int(row["id"])
        page_number = int(row["page_number"])
        key = (document_id, page_number)
        if key in contents:
            continue
        contents[key] = _confirmed_page_content(connection, document_id, page_number)
    return contents


def _confirmed_page_content(
    connection: sqlite3.Connection, document_id: int, page_number: int
) -> str:
    """Require one pages row and one page_fts row with the same full text.

    This select is not a retrieval and does not repair the index.
    """

    page_rows = connection.execute(
        "SELECT content FROM pages WHERE document_id = ? AND page_number = ?",
        (document_id, page_number),
    ).fetchall()
    index_rows = connection.execute(
        "SELECT content FROM page_fts WHERE document_id = ? AND page_number = ?",
        (document_id, page_number),
    ).fetchall()
    if len(page_rows) != 1 or len(index_rows) != 1:
        raise EvidenceCaptureError("source_index_mismatch", "页面原文与全文索引无法唯一对应。")
    page_text = page_rows[0]["content"]
    index_text = index_rows[0]["content"]
    if page_text is None or index_text is None:
        raise EvidenceCaptureError("source_index_mismatch", "页面原文与全文索引无法唯一对应。")
    page_value = str(page_text)
    index_value = str(index_text)
    if _digest(page_value) != _digest(index_value):
        raise EvidenceCaptureError("source_index_mismatch", "页面原文与全文索引内容不一致。")
    return page_value


def _assemble_packet(
    result: Mapping[str, object],
    page_contents: Mapping[tuple[int, int], str],
    products: Mapping[int, tuple[str, str]],
    alias_conflict: bool,
    provenance: _ToolProvenance,
    request: RequestContext,
    captured_at: str,
) -> EvidencePacket:
    recognized = tuple(
        RecognizedProduct(
            product_id=int(item["id"]),
            name=str(item["name"]),
            lifecycle=_product_lifecycle(products, int(item["id"])),
        )
        for item in result["recognized_products"]
    )
    evidence = tuple(
        _snapshot(row, page_contents, request.firmware_version, captured_at, products, provenance)
        for row in result["results"]
    )
    return EvidencePacket(
        packet_schema_version=PACKET_SCHEMA_VERSION,
        retrieval_state=str(result["match_state"]),
        recognized_products=recognized,
        request=request,
        alias_conflict=alias_conflict,
        evidence=evidence,
        captured_at=captured_at,
        retrieval_tool_name=provenance.tool_name,
        retrieval_tool_version=provenance.tool_version,
        retrieval_response_schema_version=provenance.response_schema_version,
    )


def _snapshot(
    row: Mapping[str, object],
    page_contents: Mapping[tuple[int, int], str],
    firmware_version: str | None,
    captured_at: str,
    products: Mapping[int, tuple[str, str]],
    provenance: _ToolProvenance,
) -> EvidenceSnapshot:
    product_id = _optional_int(row.get("canonical_product_id"))
    product_name = row.get("canonical_product_name")
    if product_name is None:
        product_name = _product_name(products, product_id)
    elif not isinstance(product_name, str):
        product_name = str(product_name)
    pdf_sha256 = str(row.get("sha256") or "")
    page_number = int(row["page_number"])
    document_id = int(row["id"])
    try:
        supporting_text = page_contents[(document_id, page_number)]
    except KeyError as exc:
        raise EvidenceCaptureError("source_index_mismatch", "页面原文与全文索引无法唯一对应。") from exc
    visible = str(row.get("snippet") or "")
    content_digest = _digest(supporting_text)
    visible_digest = _digest(visible)
    metadata = {
        "authority_level": str(row.get("authority_level") or ""),
        "canonical_product_id": product_id,
        "canonical_product_name": product_name,
        "document_lifecycle": str(row.get("status") or ""),
        "filename": str(row.get("filename") or ""),
        "firmware_applicability": classify_firmware(
            str(row.get("firmware_range") or ""), firmware_version
        ),
        "firmware_range": str(row.get("firmware_range") or ""),
        "page_number": page_number,
        "pdf_sha256": pdf_sha256,
        "product_lifecycle": _product_lifecycle(products, product_id),
        "source_locator": f"sha256:{pdf_sha256}#page={page_number}",
        "source_url": str(row.get("source_url") or ""),
    }
    metadata_digest = _digest(_canonical(metadata))
    return EvidenceSnapshot(
        evidence_id=_evidence_id(
            pdf_sha256,
            page_number,
            content_digest,
            metadata_digest,
            visible_digest,
            provenance.tool_name,
            provenance.tool_version,
            provenance.response_schema_version,
        ),
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        document_id=document_id,
        document_identity=f"sha256:{pdf_sha256}",
        filename=metadata["filename"],
        pdf_sha256=pdf_sha256,
        page_number=page_number,
        source_locator=metadata["source_locator"],
        source_url=metadata["source_url"],
        supporting_original_text=supporting_text,
        supporting_text_source=SUPPORTING_TEXT_SOURCE,
        original_content_digest=content_digest,
        metadata_digest=metadata_digest,
        canonical_product_id=product_id,
        canonical_product_name=product_name,
        product_lifecycle=metadata["product_lifecycle"],
        document_lifecycle=metadata["document_lifecycle"],
        firmware_range=metadata["firmware_range"],
        firmware_applicability=metadata["firmware_applicability"],
        authority_level=metadata["authority_level"],
        retrieval_tool_name=provenance.tool_name,
        retrieval_tool_version=provenance.tool_version,
        retrieval_response_schema_version=provenance.response_schema_version,
        captured_at=captured_at,
        decision_visible_representation=visible,
        decision_visible_digest=visible_digest,
        decision_visible_source=DECISION_VISIBLE_SOURCE,
        transformation_version=TRANSFORMATION_VERSION,
    )


def _packet_reasons(packet: EvidencePacket) -> list[str]:
    reasons: list[str] = []
    state = packet.retrieval_state
    if state == "ambiguous_product":
        reasons.append(
            REASON_AMBIGUOUS_PRODUCT_ALIAS if packet.alias_conflict else REASON_AMBIGUOUS_PRODUCT
        )
    elif state == "version_conflict":
        reasons.append(REASON_VERSION_CONFLICT)
    elif state == "insufficient_evidence":
        reasons.append(REASON_INSUFFICIENT_EVIDENCE)
    elif state == "outdated_only":
        reasons.append(REASON_OUTDATED_DOCUMENT)
    elif state == "possible_match":
        reasons.append(REASON_POSSIBLE_MATCH)

    recognized_ids = {item.product_id for item in packet.recognized_products}
    explicit_id = packet.request.explicit_product_id
    if explicit_id is not None and recognized_ids and recognized_ids != {explicit_id}:
        reasons.append(REASON_PRODUCT_FILTER_CONFLICT)
    for product in packet.recognized_products:
        reason = _product_lifecycle_reason(product.lifecycle)
        if reason:
            reasons.append(reason)
    return reasons


def _evidence_reasons(packet: EvidencePacket, evidence: EvidenceSnapshot) -> list[str]:
    reasons: list[str] = []
    recognized_ids = {item.product_id for item in packet.recognized_products}
    product_id = evidence.canonical_product_id
    if recognized_ids and product_id not in recognized_ids:
        reasons.append(REASON_EVIDENCE_PRODUCT_MISMATCH)
    explicit_id = packet.request.explicit_product_id
    if explicit_id is not None and product_id != explicit_id:
        reasons.append(REASON_PRODUCT_FILTER_CONFLICT)
    if product_id is not None:
        lifecycle_reason = _product_lifecycle_reason(evidence.product_lifecycle)
        if lifecycle_reason:
            reasons.append(lifecycle_reason)
    document_reason = _document_lifecycle_reason(evidence.document_lifecycle)
    if document_reason:
        reasons.append(document_reason)
    if evidence.firmware_applicability == "not_applicable":
        reasons.append(REASON_FIRMWARE_NOT_APPLICABLE)
    elif evidence.firmware_applicability != "applicable":
        reasons.append(REASON_FIRMWARE_UNKNOWN)
    if evidence.authority_level not in KNOWN_AUTHORITY_LEVELS:
        reasons.append(REASON_AUTHORITY_UNKNOWN)
    if not evidence.supporting_original_text.strip():
        reasons.append(REASON_INSUFFICIENT_EVIDENCE)
    return reasons


def _product_lifecycle_reason(lifecycle: str | None) -> str | None:
    """active can support. planned, inactive, archived, and unknown values cannot."""

    if lifecycle == "active":
        return None
    if lifecycle in _PRODUCT_LIFECYCLE_REASONS:
        return _PRODUCT_LIFECYCLE_REASONS[lifecycle]
    return REASON_PRODUCT_LIFECYCLE_UNKNOWN


def _document_lifecycle_reason(lifecycle: str) -> str | None:
    """effective can support.

    needs_review and draft stay visible and cannot support: they are not yet
    an issued support basis. superseded and archived cannot support either.
    Any other stored status is unknown and cannot support.
    """

    if lifecycle == "effective":
        return None
    if lifecycle in _DOCUMENT_LIFECYCLE_REASONS:
        return _DOCUMENT_LIFECYCLE_REASONS[lifecycle]
    return REASON_DOCUMENT_LIFECYCLE_UNKNOWN


def _collect_product_ids(
    result: Mapping[str, object], explicit_product_id: int | None
) -> set[int]:
    ids: set[int] = set()
    if explicit_product_id is not None:
        ids.add(explicit_product_id)
    for item in result["recognized_products"]:
        ids.add(int(item["id"]))
    for row in result["results"]:
        value = row.get("canonical_product_id")
        if value is not None:
            ids.add(int(value))
    return ids


def _load_products(
    connection: sqlite3.Connection, product_ids: set[int]
) -> dict[int, tuple[str, str]]:
    if not product_ids:
        return {}
    ordered = tuple(sorted(product_ids))
    placeholders = ",".join("?" for _ in ordered)
    rows = connection.execute(
        f"SELECT id, standard_name, status FROM products WHERE id IN ({placeholders})",
        ordered,
    ).fetchall()
    return {
        int(row["id"]): (str(row["standard_name"]), str(row["status"]))
        for row in rows
    }


def _load_alias_conflict(
    connection: sqlite3.Connection, normalized_query: str, product_ids: tuple[int, ...]
) -> bool:
    if len(product_ids) < 2:
        return False
    normalized = normalize_alias(normalized_query)
    placeholders = ",".join("?" for _ in product_ids)
    rows = connection.execute(
        f"""SELECT normalized_alias, product_id FROM product_aliases
            WHERE is_enabled = 1 AND product_id IN ({placeholders})""",
        product_ids,
    ).fetchall()
    matched: dict[str, set[int]] = {}
    for row in rows:
        alias = str(row["normalized_alias"])
        if alias and alias in normalized:
            matched.setdefault(alias, set()).add(int(row["product_id"]))
    return any(len(ids) > 1 for ids in matched.values())


def _product_name(products: Mapping[int, tuple[str, str]], product_id: int | None) -> str | None:
    if product_id is None or product_id not in products:
        return None
    return products[product_id][0]


def _product_lifecycle(
    products: Mapping[int, tuple[str, str]], product_id: int | None
) -> str | None:
    if product_id is None or product_id not in products:
        return None
    return products[product_id][1]


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evidence_id(
    pdf_sha256: str,
    page_number: int,
    content_digest: str,
    metadata_digest: str,
    decision_visible_digest: str,
    tool_name: str,
    tool_version: str,
    response_schema_version: str,
) -> str:
    body = _canonical(
        {
            "decision_visible_digest": decision_visible_digest,
            "metadata_digest": metadata_digest,
            "original_content_digest": content_digest,
            "page_number": page_number,
            "pdf_sha256": pdf_sha256,
            "response_schema_version": response_schema_version,
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "tool_name": tool_name,
            "tool_version": tool_version,
            "transformation_version": TRANSFORMATION_VERSION,
        }
    )
    return "ev1-" + hashlib.sha256(body.encode("utf-8")).hexdigest()
