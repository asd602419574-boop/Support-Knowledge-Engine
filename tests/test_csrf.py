from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support_knowledge_engine import create_app
from support_knowledge_engine.csrf import FAILURE_MESSAGE, HEADER_NAME
from support_knowledge_engine.db import connect_database
from support_knowledge_engine.demo_data import seed_demo_data
from tests.helpers import SAMPLE_DIR


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = PROJECT_ROOT / "support_knowledge_engine" / "templates"
TOKEN_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')

# Every state-changing route. IDs need not exist: the CSRF check runs before the
# view, so a rejected request never reaches the database lookup.
POST_ROUTES = (
    "/imports",
    "/documents/1/edit",
    "/products",
    "/products/1/edit",
    "/products/1/aliases",
    "/aliases/1/toggle",
)

PRODUCT_FORM = {
    "standard_name": "AeroCam Mini 2",
    "product_series": "AeroCam",
    "status": "active",
    "reason": "测试创建产品",
}


class CsrfProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "csrf.db"
        self.app = create_app(
            {"TESTING": True, "DATABASE": str(self.database), "OPERATOR_NAME": "虚构测试操作者"}
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def _product_count(self) -> int:
        with connect_database(self.database) as connection:
            return connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    def _token(self, client=None, path: str = "/products") -> str:
        response = (client or self.client).get(path)
        self.assertEqual(response.status_code, 200)
        match = TOKEN_PATTERN.search(response.get_data(as_text=True))
        self.assertIsNotNone(match, f"{path} 页面没有渲染 csrf_token 隐藏字段")
        return match.group(1)

    def test_every_post_route_rejects_a_request_without_a_token(self):
        for path in POST_ROUTES:
            with self.subTest(path=path):
                response = self.client.post(path, data={"reason": "x"})
                self.assertEqual(response.status_code, 400)
                self.assertIn(FAILURE_MESSAGE, response.get_data(as_text=True))

    def test_rejected_request_does_not_change_the_database(self):
        self.client.get("/products")  # a session exists, but the form sends no token
        response = self.client.post("/products", data=PRODUCT_FORM)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._product_count(), 0)

    def test_wrong_and_non_ascii_tokens_are_rejected_without_a_server_error(self):
        self._token()
        for bad in ("wrong-token", "令牌错误", "", " "):
            with self.subTest(token=bad):
                response = self.client.post("/products", data={**PRODUCT_FORM, "csrf_token": bad})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self._product_count(), 0)

    def test_valid_token_in_the_form_is_accepted(self):
        token = self._token()
        response = self.client.post("/products", data={**PRODUCT_FORM, "csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._product_count(), 1)

    def test_valid_token_in_the_header_is_accepted(self):
        token = self._token()
        response = self.client.post("/products", data=PRODUCT_FORM, headers={HEADER_NAME: token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._product_count(), 1)

    def test_token_is_stable_within_a_session_and_not_valid_in_another(self):
        first = self._token()
        self.assertEqual(self._token(), first)

        other_client = self.app.test_client()
        other_token = self._token(other_client)
        self.assertNotEqual(first, other_token)

        # A token read from one browser session must not work in another one.
        response = other_client.post("/products", data={**PRODUCT_FORM, "csrf_token": first})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._product_count(), 0)

    def test_post_without_any_session_is_rejected_even_with_a_token_value(self):
        fresh_client = self.app.test_client()  # never loaded a page, so it has no session token
        response = fresh_client.post(
            "/products", data={**PRODUCT_FORM, "csrf_token": "guessed-by-attacker"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._product_count(), 0)

    def test_read_only_pages_do_not_require_a_token(self):
        for path in ("/", "/products", "/audit", "/logs", "/favicon.ico"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_session_cookie_is_lax_samesite_and_httponly(self):
        # SameSite=Lax keeps browsers from attaching the cookie to cross-site
        # POSTs, a second layer on top of the token check.
        headers = self.app.test_client().get("/products").headers.getlist("Set-Cookie")
        session_cookies = [value for value in headers if value.startswith("session=")]
        self.assertEqual(len(session_cookies), 1)
        self.assertIn("SameSite=Lax", session_cookies[0])
        self.assertIn("HttpOnly", session_cookies[0])


class SecretKeyTests(unittest.TestCase):
    def _create(self, directory: str, name: str):
        return create_app({"TESTING": True, "DATABASE": str(Path(directory) / name)})

    def test_secret_key_is_random_per_process_and_never_the_public_default(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ):
            os.environ.pop("SUPPORT_KE_SECRET", None)
            first = self._create(directory, "a.db").config["SECRET_KEY"]
            second = self._create(directory, "b.db").config["SECRET_KEY"]
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, "local-development-only")
        self.assertGreaterEqual(len(first), 32)

    def test_configured_secret_is_used_so_sessions_can_survive_restarts(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"SUPPORT_KE_SECRET": "operator-chosen-secret"}
        ):
            key = self._create(directory, "c.db").config["SECRET_KEY"]
        self.assertEqual(key, "operator-chosen-secret")


class RenderedFormsTests(unittest.TestCase):
    """Drive the real pages: every POST form on them must submit successfully."""

    FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.DOTALL | re.IGNORECASE)
    HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)">')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "forms.db"
        seed_demo_data(self.database, SAMPLE_DIR)
        self.app = create_app(
            {"TESTING": True, "DATABASE": str(self.database), "OPERATOR_NAME": "虚构测试操作者"}
        )
        self.client = self.app.test_client()
        with connect_database(self.database) as connection:
            self.product_id = connection.execute("SELECT MIN(id) FROM products").fetchone()[0]
            self.document_id = connection.execute("SELECT MIN(id) FROM documents").fetchone()[0]

    def tearDown(self):
        self.temp.cleanup()

    def _post_forms(self, path: str):
        html = self.client.get(path).get_data(as_text=True)
        forms = []
        for attributes, body in self.FORM.findall(html):
            if 'method="post"' not in attributes.lower():
                continue
            action = re.search(r'action="([^"]+)"', attributes).group(1)
            forms.append((action, dict(self.HIDDEN.findall(body))))
        return forms

    def test_each_rendered_post_form_is_accepted_with_its_own_token_and_rejected_without(self):
        pages = (
            "/",
            "/products",
            f"/products/{self.product_id}",
            f"/documents/{self.document_id}",
        )
        seen_actions = set()
        for page in pages:
            forms = self._post_forms(page)
            for action, hidden in forms:
                seen_actions.add(re.sub(r"/\d+", "/<id>", action))
                with self.subTest(page=page, action=action):
                    self.assertIn("csrf_token", hidden, "表单缺少 csrf_token 隐藏字段")
                    # Token accepted: the view runs and answers with a redirect
                    # (validation errors in these empty forms redirect as well).
                    accepted = self.client.post(action, data=hidden)
                    self.assertNotEqual(accepted.status_code, 400)
                    self.assertEqual(accepted.status_code, 302)

                    without = {key: value for key, value in hidden.items() if key != "csrf_token"}
                    rejected = self.client.post(action, data=without)
                    self.assertEqual(rejected.status_code, 400)
        # The pages above expose all six state-changing routes.
        self.assertEqual(
            seen_actions,
            {
                "/imports",
                "/products",
                "/products/<id>/edit",
                "/products/<id>/aliases",
                "/aliases/<id>/toggle",
                "/documents/<id>/edit",
            },
        )


class TemplateCoverageTests(unittest.TestCase):
    def test_every_post_form_in_the_templates_carries_the_token(self):
        # Guards future work: a new <form method="post"> without the hidden
        # field would otherwise only fail at runtime, for a real user.
        form = re.compile(r"<form\b[^>]*\bmethod=\"post\"[^>]*>(.*?)</form>", re.DOTALL | re.IGNORECASE)
        found = 0
        for template in sorted(TEMPLATES.glob("*.html")):
            for body in form.findall(template.read_text(encoding="utf-8")):
                found += 1
                with self.subTest(template=template.name):
                    self.assertIn('name="csrf_token"', body)
                    self.assertIn("csrf_token()", body)
        self.assertEqual(found, len(POST_ROUTES), "POST 表单数量与 POST 路由数量应一致")


if __name__ == "__main__":
    unittest.main()
