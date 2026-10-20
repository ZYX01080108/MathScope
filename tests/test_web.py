import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api import create_app
from src.settings import Settings
from src.deepseek_client import DeepSeekSettings


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.root = Path(temporary) / "data"
        self.app = create_app(Settings(data_dir=self.root))
        self.client = self.enterContext(TestClient(self.app))

    def test_home_contains_forms_and_same_origin_assets(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        for marker in ('lang="zh-CN"', 'id="upload-form"', 'id="parse-form"', 'id="search-form"', 'id="results"', '/ui/app.js', '/ui/app.css'):
            self.assertIn(marker, response.text)
        for marker in ('id="preview"', 'id="external-consent"', 'id="answer"', 'id="model-status"', 'id="answer-result"'):
            self.assertIn(marker, response.text)
        self.assertIn("只允许选择核对过的结论", response.text)
        self.assertNotIn('type="password"', response.text)

    def test_assets_have_explicit_content_types_and_security_headers(self):
        for path, media_type in (("/", "text/html"), ("/ui/app.css", "text/css"), ("/ui/app.js", "application/javascript")):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(media_type, response.headers["content-type"])
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertIn("script-src 'self'", response.headers["content-security-policy"])
                self.assertNotIn("unsafe-inline", response.headers["content-security-policy"])

    def test_page_and_assets_do_not_create_data_or_indexes(self):
        for path in ("/", "/ui/app.css", "/ui/app.js"):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.app.state.document_search._cache)

    def test_ui_routes_are_not_business_openapi_endpoints(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertEqual(set(paths), {"/health", "/generation-status", "/documents", "/documents/{document_id}", "/documents/{document_id}/parse", "/documents/{document_id}/search", "/documents/{document_id}/file", "/documents/{document_id}/context", "/documents/{document_id}/check-answer", "/documents/{document_id}/answer", "/documents/{document_id}/quality", "/documents/{document_id}/reviewed-context", "/documents/{document_id}/answer-reviewed", "/search"})
        self.assertEqual(sum(len(methods) for methods in paths.values()), 15)
        self.assertEqual(self.client.get("/docs").status_code, 200)

    def test_private_files_and_unlisted_assets_are_not_served(self):
        for path in ("/ui/api.py", "/ui/.env", "/ui/../api.py", "/ui/%2e%2e/api.py", "/src/api.py", "/data/raw/lecture.pdf"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.post("/").status_code, 405)

    def test_model_status_is_free_and_never_exposes_a_credential(self):
        settings = DeepSeekSettings(enabled=True, api_key="test-secret-never-return-this", max_calls=3)
        app = create_app(Settings(data_dir=self.root), deepseek_settings=settings)
        with TestClient(app) as client, patch.object(app.state.deepseek, "complete", side_effect=AssertionError("no paid call")):
            for _ in range(2):
                response = client.get("/generation-status")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.json(), {"provider": "deepseek", "model": "deepseek-flash", "status": "ready",
                                                  "calls_used": 0, "max_calls": 3, "credential_verified": False})
                self.assertNotIn(settings.api_key, response.text)
                self.assertNotIn("api_key", response.text)
            self.assertFalse(self.root.exists())

    def test_model_status_distinguishes_local_unavailability_without_upstream_calls(self):
        cases = [(DeepSeekSettings(), 0, False, "disabled"),
                 (DeepSeekSettings(enabled=True), 0, False, "missing_key"),
                 (DeepSeekSettings(enabled=True, api_key="test-only", max_calls=1), 1, False, "limit_reached"),
                 (DeepSeekSettings(enabled=True, api_key="test-only"), 0, True, "busy")]
        for settings, count, busy, expected in cases:
            with self.subTest(expected=expected), TestClient(create_app(Settings(data_dir=self.root), deepseek_settings=settings)) as client:
                client.app.state.deepseek._calls_used = count
                client.app.state.deepseek._in_flight = busy
                with patch.object(client.app.state.deepseek, "_request", side_effect=AssertionError("no network")):
                    body = client.get("/generation-status").json()
                self.assertEqual(body["status"], expected)
                self.assertEqual(body["calls_used"], count)

    def test_script_uses_text_nodes_for_untrusted_content(self):
        script = self.client.get("/ui/app.js").text
        self.assertIn("result.textContent = String(text)", script)
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval("):
            self.assertNotIn(unsafe, script)
        self.assertIn("不代表文档没有答案", script)
        self.assertIn("不要重复提交", script)

    def test_upload_parse_search_flow_preserves_literal_source_text(self):
        uploaded = self.client.post("/documents", files={"file": ("demo.pdf", b"%PDF-1.7\nfixture", "application/pdf")})
        self.assertEqual(uploaded.status_code, 201)
        document_id = uploaded.json()["document_id"]
        text = 'alpha <img src=x onerror="alert(1)">\nOriginal source.'
        reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: text)], is_encrypted=False)
        with patch("src.document_processing.PdfReader", return_value=reader):
            parsed = self.client.post(f"/documents/{document_id}/parse", json={"chunk_size": 500})
        self.assertEqual(parsed.status_code, 200)
        searched = self.client.post(f"/documents/{document_id}/search", json={"query": "alpha", "method": "normalized", "k": 5})
        self.assertEqual(searched.status_code, 200)
        self.assertEqual(searched.json()["results"][0]["chunk"]["text"], text)
        self.assertEqual(self.client.get("/documents").json()["documents"][0]["status"], "parsed")
        self.assertEqual(self.client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
