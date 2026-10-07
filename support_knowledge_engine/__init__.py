from __future__ import annotations

import os
import secrets
from pathlib import Path

from flask import Flask

from .csrf import init_csrf
from .db import init_database
from .governance import (
    ALIAS_TYPE_LABELS,
    AUTHORITY_LEVEL_LABELS,
    DOCUMENT_STATUS_LABELS,
    PRODUCT_STATUS_LABELS,
)
from .routes import bp


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        # A fixed default would be public in the repository, letting anyone forge
        # the session cookie that carries the CSRF token. Generate one per run
        # instead; set SUPPORT_KE_SECRET to keep sessions across restarts.
        SECRET_KEY=os.environ.get("SUPPORT_KE_SECRET") or secrets.token_hex(32),
        SESSION_COOKIE_SAMESITE="Lax",
        DATABASE=os.environ.get("SUPPORT_KE_DATABASE") or str(Path(app.instance_path) / "knowledge.db"),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        OPERATOR_NAME=os.environ.get("SUPPORT_KE_OPERATOR")
        or os.environ.get("USERNAME")
        or "本地维护者",
    )

    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    init_database(app.config["DATABASE"])
    init_csrf(app)
    app.register_blueprint(bp)

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
