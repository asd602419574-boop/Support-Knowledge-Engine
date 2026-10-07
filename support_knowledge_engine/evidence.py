"""Immutable evidence packet and evidence decision.

Retrieval state is a signal. It is not an evidence decision. This module
captures one retrieval inside a single read transaction, freezes the rows it
already returned, and decides only from that packet. Decision never queries
documents, pages, or products again.

The current corpus stores firmware_range as an empty string. Empty means no
restriction was stored. The only positive grammar is exact-dotted-v1:
``MAJOR.MINOR.PATCH`` with numeric components. Every other non-empty value,
including the governance placeholder ``2.0.0 - 2.9.x``, is unknown.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from . import repository
from .governance import AUTHORITY_LEVEL_LABELS, normalize_alias
from .retrieval_tool import RESPONSE_SCHEMA_VERSION, TOOL_NAME, TOOL_VERSION


SNAPSHOT_SCHEMA_VERSION = "1"
PACKET_SCHEMA_VERSION = "1"
TRANSFORMATION_IDENTITY = "identity-v1"
SUPPORTING_TEXT_SOURCE = "retrieval-snippet"
FIRMWARE_GRAMMAR = "exact-dotted-v1"

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
_EXACT_DOTTED_VERSION = re.compile(r"^[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}$")
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


@dataclass(frozen=True)
class RecognizedProduct:
    product_id: int
    name: str
    lifecycle: str | None


@dataclass(frozen=True)
class RequestContext:
    """Caller context frozen with the packet. It is not read back from the database."""

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


def classify_firmware(firmware_range: str, firmware_version: str | None) -> str:
    """Return applicable, not_applicable, or unknown.

    Empty range means the document has no stored restriction. exact-dotted-v1
    compares the whole string. Ranges, wildcards, and other text stay unknown.
    """

    stored = firmware_range.strip()
    if stored == "":
        return "applicable"
    if _EXACT_DOTTED_VERSION.fullmatch(stored) is None:
        return "unknown"
    supplied = "" if firmware_version is None else firmware_version.strip()
    if supplied == "":
        return "unknown"
    if supplied == stored:
        return "applicable"
    return "not_applicable"


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
) -> EvidencePacket:
    """Retrieve once and freeze the packet before the read transaction ends.

    Page text and document metadata come from that retrieval result. Product
    lifecycle and alias conflict are read in the same transaction, then the
    transaction this function opened is rolled back. A caller transaction is
    left alone. No telemetry write and no second retrieval.
    """

    timestamp = captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _read_boundary(connection):
        result = repository.retrieve_with_context(
            connection,
            query,
            product_series,
            document_type,
            status,
            association,
            product_id,
        )
        explicit_product_id = int(product_id) if product_id.isdigit() else None
        product_ids = _collect_product_ids(result, explicit_product_id)
        products = _load_products(connection, product_ids)
        alias_conflict = _load_alias_conflict(
            connection,
            str(result["normalized_query"]),
            tuple(int(item["id"]) for item in result["recognized_products"]),
        )
        return _assemble_packet(
            result,
            products,
            alias_conflict,
            RequestContext(
                explicit_product_id=explicit_product_id,
                explicit_product_name=_product_name(products, explicit_product_id),
                explicit_product_lifecycle=_product_lifecycle(products, explicit_product_id),
                firmware_version=None if firmware_version is None else firmware_version.strip(),
                product_series=product_series,
                document_type=document_type,
                status=status,
                association=association,
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


def _assemble_packet(
    result: Mapping[str, object],
    products: Mapping[int, tuple[str, str]],
    alias_conflict: bool,
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
        _snapshot(row, request.firmware_version, captured_at, products)
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
        retrieval_tool_name=TOOL_NAME,
        retrieval_tool_version=TOOL_VERSION,
        retrieval_response_schema_version=RESPONSE_SCHEMA_VERSION,
    )


def _snapshot(
    row: Mapping[str, object],
    firmware_version: str | None,
    captured_at: str,
    products: Mapping[int, tuple[str, str]],
) -> EvidenceSnapshot:
    product_id = _optional_int(row.get("canonical_product_id"))
    product_name = row.get("canonical_product_name")
    if product_name is None:
        product_name = _product_name(products, product_id)
    elif not isinstance(product_name, str):
        product_name = str(product_name)
    pdf_sha256 = str(row.get("sha256") or "")
    page_number = int(row["page_number"])
    supporting_text = str(row.get("snippet") or "")
    content_digest = _digest(supporting_text)
    visible = supporting_text
    metadata = {
        "authority_level": str(row.get("authority_level") or ""),
        "canonical_product_id": product_id,
        "canonical_product_name": product_name,
        "document_lifecycle": str(row.get("status") or ""),
        "filename": str(row.get("filename") or ""),
        "firmware_applicability": classify_firmware(str(row.get("firmware_range") or ""), firmware_version),
        "firmware_range": str(row.get("firmware_range") or ""),
        "page_number": page_number,
        "pdf_sha256": pdf_sha256,
        "product_lifecycle": _product_lifecycle(products, product_id),
        "source_locator": f"sha256:{pdf_sha256}#page={page_number}",
        "source_url": str(row.get("source_url") or ""),
    }
    metadata_digest = _digest(_canonical(metadata))
    return EvidenceSnapshot(
        evidence_id=_evidence_id(pdf_sha256, page_number, content_digest, metadata_digest),
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        document_id=int(row["id"]),
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
        retrieval_tool_name=TOOL_NAME,
        retrieval_tool_version=TOOL_VERSION,
        retrieval_response_schema_version=RESPONSE_SCHEMA_VERSION,
        captured_at=captured_at,
        decision_visible_representation=visible,
        decision_visible_digest=_digest(visible),
        transformation_version=TRANSFORMATION_IDENTITY,
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
    pdf_sha256: str, page_number: int, content_digest: str, metadata_digest: str
) -> str:
    body = _canonical(
        {
            "metadata_digest": metadata_digest,
            "original_content_digest": content_digest,
            "page_number": page_number,
            "pdf_sha256": pdf_sha256,
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "transformation_version": TRANSFORMATION_IDENTITY,
        }
    )
    return "ev1-" + hashlib.sha256(body.encode("utf-8")).hexdigest()
