from __future__ import annotations


def document_form_values(document, **overrides: str) -> dict[str, str]:
    values = {
        "title": document["title"],
        "product_series": document["product_series"],
        "product_model": document["product_model"],
        "document_type": document["document_type"],
        "language": document["language"],
        "version": document["version"],
        "release_date": document["release_date"],
        "source_url": document["source_url"],
        "status": document["status"],
        "canonical_product_id": str(document["canonical_product_id"] or ""),
        "superseded_by_document_id": str(document["superseded_by_document_id"] or ""),
        "effective_date": document["effective_date"] or "",
        "expiration_date": document["expiration_date"] or "",
        "firmware_range": document["firmware_range"],
        "authority_level": document["authority_level"],
        "status_note": document["status_note"],
    }
    values.update(overrides)
    return values
