"""Session-bound CSRF protection for every state-changing request.

The application only listens on the loopback address, but a malicious web page
open in the same browser can still make the browser send a POST to
``127.0.0.1:5000``. A per-session token that the attacking page cannot read
closes that gap without adding a dependency.

The token lives in Flask's signed session cookie, so it is only as strong as
``SECRET_KEY``: ``create_app`` therefore generates a random key per process
unless ``SUPPORT_KE_SECRET`` is set.
"""

from __future__ import annotations

import hmac
import secrets

from flask import Flask, abort, request, session


SESSION_KEY = "_csrf_token"
FORM_FIELD = "csrf_token"
HEADER_NAME = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
FAILURE_MESSAGE = (
    "页面已过期或请求来源无法确认（CSRF 校验失败）。请返回上一页，刷新后重新提交。"
)


def csrf_token() -> str:
    """Return the session's token, creating it on first use."""
    token = session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_KEY] = token
    return token


def validate_csrf() -> None:
    """Reject unsafe requests whose token is missing or does not match."""
    if request.method in SAFE_METHODS:
        return
    expected = session.get(SESSION_KEY)
    submitted = request.form.get(FORM_FIELD) or request.headers.get(HEADER_NAME)
    # Compare bytes: str comparison raises TypeError on non-ASCII input, which
    # an attacker controls and would turn into a 500 instead of a 400.
    if (
        not expected
        or not submitted
        or not hmac.compare_digest(str(expected).encode("utf-8"), submitted.encode("utf-8"))
    ):
        abort(400, description=FAILURE_MESSAGE)


def init_csrf(app: Flask) -> None:
    app.before_request(validate_csrf)
    app.jinja_env.globals["csrf_token"] = csrf_token
