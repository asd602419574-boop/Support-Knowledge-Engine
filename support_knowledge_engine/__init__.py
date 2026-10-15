from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from .db import init_database
from .governance import (
    ALIAS_TYPE_LABELS,
    AUTHORITY_LEVEL_LABELS,
    DOCUMENT_STATUS_LABELS,
    PRODUCT_STATUS_LABELS,
)
from .routes import bp
from .assistant import bp as assistant_bp
from .pcn_review import bp as pcn_bp
from .integration import bp as integration_bp


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SUPPORT_KE_SECRET", "local-development-only"),
        DATABASE=os.environ.get("SUPPORT_KE_DATABASE") or str(Path(app.instance_path) / "knowledge.db"),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        OPERATOR_NAME=os.environ.get("SUPPORT_KE_OPERATOR")
        or os.environ.get("USERNAME")
        or "本地维护者",
        LLM_BASE_URL=os.environ.get("SUPPORT_KE_LLM_BASE_URL", ""),
        LLM_API_KEY=os.environ.get("SUPPORT_KE_LLM_API_KEY", ""),
        LLM_MODEL=os.environ.get("SUPPORT_KE_LLM_MODEL", ""),
        INTEGRATION_TOKEN=os.environ.get("SUPPORT_KE_INTEGRATION_TOKEN", ""),
        INTEGRATION_SITE_ID=os.environ.get("SUPPORT_KE_INTEGRATION_SITE_ID", ""),
        LLM_TOKEN_LIMIT_FIELD=os.environ.get("SUPPORT_KE_LLM_TOKEN_LIMIT_FIELD", "max_tokens"),
    )

    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    init_database(app.config["DATABASE"])
    app.register_blueprint(bp)
    app.register_blueprint(assistant_bp)
    app.register_blueprint(pcn_bp)
    app.register_blueprint(integration_bp)

    @app.context_processor
    def governance_labels() -> dict[str, object]:
        return {
            "document_status_labels": DOCUMENT_STATUS_LABELS,
            "product_status_labels": PRODUCT_STATUS_LABELS,
            "alias_type_labels": ALIAS_TYPE_LABELS,
            "authority_level_labels": AUTHORITY_LEVEL_LABELS,
            "operator_name": app.config["OPERATOR_NAME"],
        }

    return app
