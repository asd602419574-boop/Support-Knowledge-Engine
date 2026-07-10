from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from .db import init_database
from .routes import bp


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SUPPORT_KE_SECRET", "local-development-only"),
        DATABASE=str(Path(app.instance_path) / "knowledge.db"),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )

    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    init_database(app.config["DATABASE"])
    app.register_blueprint(bp)

    return app

