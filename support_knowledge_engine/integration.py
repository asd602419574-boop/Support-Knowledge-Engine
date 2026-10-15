"""Versioned, authenticated projection of the existing deterministic runtime.

One instance serves one site/corpus. No browser session or model is involved.
"""
from __future__ import annotations

import hmac
import sqlite3
from datetime import date

from flask import Blueprint, current_app, jsonify, request

from .db import connect_database
from .evidence import snapshot_integrity_ok
from .runtime import RuntimeRequest, run_runtime

bp = Blueprint("integration", __name__, url_prefix="/api/integration/evidence")
VERSION = "1"
NOTICE = "supported 仅表示本次证据判定得到支持，不代表厂商授权或完整步骤语义验证；正式政策仍须核实来源身份及适用范围。"


def envelope(status, *, code="", **fields):
    return dict(contract_version=VERSION, status=status, items=[], match_state=None,
                decision=None, reason_code=code, warnings=[NOTICE],
                provenance={"service": "ske", "site_id": current_app.config["INTEGRATION_SITE_ID"],
                            "contract_version": VERSION}, **fields)


@bp.before_request
def authenticate():
    secret = current_app.config["INTEGRATION_TOKEN"]
    if not secret or not current_app.config["INTEGRATION_SITE_ID"]:
        return jsonify(envelope("unavailable", code="integration_not_configured")), 503
    supplied = request.headers.get("Authorization", "")
    if not hmac.compare_digest(supplied.encode(), ("Bearer " + secret).encode()):
        return jsonify(envelope("unavailable", code="unauthorized")), 401


@bp.post("/test")
def test_connection():
    # Authenticated liveness + DB readability; empty corpus is still connected.
    try:
        with connect_database(current_app.config["DATABASE"]) as conn:
            conn.execute("SELECT id FROM documents LIMIT 1").fetchone()
        return jsonify(envelope("ok"))
    except sqlite3.Error:
        return jsonify(envelope("unavailable", code="database_unavailable")), 503


@bp.post("/query")
def query():
    data = request.get_json(silent=True)
    allowed = {"contract_version", "site_id", "query", "product_id", "product_alias",
               "firmware_version", "document_type", "max_results"}
    if (not isinstance(data, dict) or set(data) - allowed
            or data.get("contract_version") != VERSION
            or data.get("site_id") != current_app.config["INTEGRATION_SITE_ID"]):
        return jsonify(envelope("invalid_source", code="invalid_request")), 400
    for key in ("query", "product_id", "product_alias", "document_type", "firmware_version"):
        value = data.get(key, "")
        if not isinstance(value, str) or len(value) > (1000 if key == "query" else 120):
            return jsonify(envelope("invalid_source", code="invalid_request")), 400
    limit = data.get("max_results", 8)
    if (type(limit) is not int or not 1 <= limit <= 20 or not data.get("query", "").strip()
            or data.get("product_id") and not data["product_id"].isdigit()):
        return jsonify(envelope("invalid_source", code="invalid_request")), 400
    try:
        with connect_database(current_app.config["DATABASE"]) as conn:
            # Enrichment and the packet share a read snapshot. Never re-retrieve.
            conn.execute("BEGIN DEFERRED")
            result = run_runtime(conn, RuntimeRequest(
                request_schema_version="1", query=(data.get("product_alias", "") + " " + data["query"]).strip(),
                product_id=data.get("product_id", ""), document_type=data.get("document_type", ""),
                firmware_version=data.get("firmware_version")), retrieval_deadline_s=3.0)
            if not result.ok:
                code = result.error.type
                return jsonify(envelope("unavailable", code=code)), 503
            out = envelope("ok")
            out.update(match_state=result.packet.retrieval_state,
                       decision=result.decision.decision_type,
                       reason_code=list(result.decision.reason_codes),
                       runtime_decision=result.decision.decision_type)
            out["provenance"].update(runtime_version=result.runtime_version,
                                     packet_schema_version=result.packet.packet_schema_version,
                                     captured_at=result.packet.captured_at)
            items = []
            for evidence in result.packet.evidence:
                if not snapshot_integrity_ok(evidence):
                    return jsonify(envelope("invalid_source", code="evidence_integrity_failed")), 500
                doc = conn.execute("SELECT * FROM documents WHERE id=?", (evidence.document_id,)).fetchone()
                restrictions = list(result.decision.reason_codes)
                today = date.today().isoformat()
                if doc["expiration_date"] and doc["expiration_date"] < today:
                    restrictions.append("document_expired")
                if doc["effective_date"] and doc["effective_date"] > today:
                    restrictions.append("document_not_yet_effective")
                if any(c in restrictions for c in ("document_expired", "document_not_yet_effective")):
                    # Runtime v1 does not interpret dates. Conservative API restriction,
                    # preserving runtime_decision for traceability.
                    out["decision"] = "abstain" if out["decision"] != "conflict" else "conflict"
                    out["reason_code"] = sorted(set(out["reason_code"] + restrictions))
                items.append(dict(
                    evidence_id=evidence.evidence_id, document_id=evidence.document_id,
                    title=doc["title"], document_type=doc["document_type"], version=doc["version"],
                    lifecycle=evidence.document_lifecycle, release_date=doc["release_date"],
                    effective_date=doc["effective_date"], expiration_date=doc["expiration_date"],
                    product_id=evidence.canonical_product_id, product=evidence.canonical_product_name,
                    product_lifecycle=evidence.product_lifecycle, firmware_range=evidence.firmware_range,
                    firmware_applicability=evidence.firmware_applicability,
                    page_number=evidence.page_number, original_excerpt=evidence.supporting_original_text[:4000],
                    pdf_sha256=evidence.pdf_sha256, source_identity=evidence.document_identity,
                    source_locator=evidence.source_locator, authority_level=evidence.authority_level,
                    applicability=result.decision.decision_type if not restrictions else "restricted",
                    reason_codes=restrictions, official_authorization=False))
            out["items"] = items[:limit]
            out["presentation_truncated"] = len(items) > limit
            out["status"] = ("conflict" if out["decision"] == "conflict" else
                             "abstain" if items and out["decision"] == "abstain" else
                             "ok" if items else "no_match")
            return jsonify(out)
    except sqlite3.Error:
        return jsonify(envelope("unavailable", code="database_unavailable")), 503
