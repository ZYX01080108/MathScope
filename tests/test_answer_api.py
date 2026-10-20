import hashlib
import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from src.api import create_app
from src.deepseek_client import DeepSeekClient, DeepSeekSettings
from src.settings import Settings


class AnswerApiTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = Settings(data_dir=self.root / "data")
        self.model_settings = DeepSeekSettings(enabled=True, api_key="offline-fixture-secret", max_calls=3)
        self.app = create_app(self.settings, deepseek_settings=self.model_settings)
        self.client = self.enterContext(TestClient(self.app))
        self.document_id = self.upload_and_parse()
        self.url = f"/documents/{self.document_id}/answer"
        self.requests = []
        self.draft = {"decision": "answer", "claims": [{"text": "测试模型要点。", "citations": [{"citation_id": "E1", "quote": "alpha beta"}]}], "reason": None}
        self.install_transport()

    def upload_and_parse(self, text="alpha beta 第一页", parse=True):
        # A valid in-memory PDF with no vector marks. Extraction text remains
        # an explicit test double; the new graphics inspection uses real bytes.
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        stream = BytesIO()
        writer.write(stream)
        uploaded = self.client.post("/documents", files={"file": ("fixture.pdf", stream.getvalue(), "application/pdf")}).json()
        document_id = uploaded["document_id"]
        if parse:
            reader = SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: text)])
            with patch("src.document_processing.PdfReader", return_value=reader):
                response = self.client.post(f"/documents/{document_id}/parse", json={"chunk_size": 500})
            self.assertEqual(response.status_code, 200)
        return document_id

    def install_transport(self, callback=None, settings=None):
        def handle(request):
            self.requests.append(request)
            if callback:
                return callback(request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(self.draft)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
        self.app.state.deepseek = DeepSeekClient(settings or self.model_settings, transport=httpx.MockTransport(handle))

    def answer(self, **changes):
        return self.client.post(self.url, json={"query": "alpha", "allow_external": True, **changes})

    def test_answer_flow_returns_checked_sources_with_model_usage(self):
        response = self.answer()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "answered")
        self.assertEqual(body["document_id"], self.document_id)
        self.assertEqual(body["check"]["status"], "passed")
        self.assertFalse(body["check"]["semantics_verified"])
        self.assertEqual(body["check"]["claims"][0]["citations"][0]["pdf_page_number"], 1)
        self.assertEqual(body["generation"]["usage"]["total_tokens"], 120)
        self.assertEqual(body["generation"]["calls_used"], 1)
        packet = self.client.post(f"/documents/{self.document_id}/context", json={"query": "alpha", "method": "normalized", "k": 10}).json()
        self.assertEqual(body["context_id"], packet["context_id"])
        self.assertEqual(len(self.requests), 1)

    def test_consent_is_required_and_not_coerced_from_numeric_or_string_values(self):
        for value in (None, False, 1, "true", "1", [], {}):
            payload = {"query": "alpha"}
            if value is not None:
                payload["allow_external"] = value
            with self.subTest(value=value):
                self.assertEqual(self.client.post(self.url, json=payload).status_code, 422)
        self.assertEqual(self.requests, [])

    def test_bad_parameters_or_caller_supplied_key_source_or_destination_are_rejected(self):
        for changes in ({"query": " "}, {"query": "!!!"}, {"k": True}, {"k": 21},
                        {"max_context_chars": 6001}, {"max_context_chars": "6000"}, {"method": "semantic"},
                        {"api_key": "not-accepted"}, {"base_url": "https://evil.example"}, {"evidence": []},
                        {"document_path": "private"}, {"draft": self.draft}):
            with self.subTest(changes=changes):
                self.assertEqual(self.answer(**changes).status_code, 422)
        self.assertEqual(self.requests, [])

    def test_no_matches_and_budget_exceeded_skip_model(self):
        missing = self.answer(query="nonmatchinguniquephrase").json()
        small = self.answer(max_context_chars=128).json()
        self.assertEqual(missing["context_status"], "no_matches")
        self.assertEqual(small["context_status"], "budget_exceeded")
        for body in (missing, small):
            self.assertEqual(body["status"], "refused")
            self.assertFalse(body["generation"]["model_called"])
            self.assertIsNone(body["generation"]["usage"])
            self.assertEqual(body["generation"]["calls_used"], 0)
        self.assertEqual(self.requests, [])

    def test_unconfigured_generation_does_not_break_existing_services(self):
        self.install_transport(settings=DeepSeekSettings())
        self.assertEqual(self.answer().status_code, 503)
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/documents").status_code, 200)
        self.assertEqual(self.client.post(f"/documents/{self.document_id}/search", json={"query": "alpha"}).status_code, 200)
        self.assertEqual(self.requests, [])

    def test_model_generated_bad_reference_is_200_rejected_with_no_claims(self):
        self.draft["claims"][0]["citations"][0]["citation_id"] = "E999"
        response = self.answer()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "rejected")
        self.assertEqual(response.json()["check"]["claims"], [])
        self.assertEqual(response.json()["check"]["issues"][0]["code"], "unknown_citation")
        self.assertTrue(response.json()["generation"]["model_called"])

    def test_model_cannot_supply_its_own_source_locations(self):
        self.draft["claims"][0]["citations"][0]["pdf_page_number"] = 999
        body = self.answer().json()
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(body["check"]["claims"], [])

    def test_model_refusal_and_empty_answer_are_distinct(self):
        self.draft = {"decision": "refuse", "claims": [], "reason": "缺少完整证明"}
        body = self.answer().json()
        self.assertEqual(body["status"], "refused")
        self.assertEqual(body["check"]["refusal_reason"], "缺少完整证明")
        self.draft = {"decision": "answer", "claims": []}
        self.assertEqual(self.answer().json()["status"], "rejected")

    def test_network_or_upstream_errors_are_sanitized_without_retries(self):
        for code, expected in ((401, 503), (402, 503), (429, 503), (500, 502)):
            self.requests.clear()
            self.install_transport(lambda request, code=code: httpx.Response(code, json={"secret": "offline-fixture-secret"}))
            response = self.answer()
            self.assertEqual(response.status_code, expected)
            self.assertNotIn("offline-fixture-secret", response.text)
            self.assertEqual(len(self.requests), 1)
        def timeout(request):
            raise httpx.ReadTimeout("offline-fixture-secret", request=request)
        self.requests.clear()
        self.install_transport(timeout)
        response = self.answer()
        self.assertEqual(response.status_code, 504)
        self.assertNotIn("offline-fixture-secret", response.text)
        self.assertEqual(len(self.requests), 1)

    def test_invalid_json_or_cut_off_content_is_not_returned(self):
        self.install_transport(lambda request: httpx.Response(200, content=b"private-invalid-upstream-response"))
        response = self.answer()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private-invalid", response.text)

    def test_call_limit_blocks_additional_upstream_attempts(self):
        self.install_transport(settings=DeepSeekSettings(enabled=True, api_key="offline-fixture-secret", max_calls=1))
        self.assertEqual(self.answer().status_code, 200)
        self.assertEqual(self.answer().status_code, 429)
        self.assertEqual(len(self.requests), 1)

    def test_missing_unparsed_and_wrong_document_never_reach_model(self):
        self.assertEqual(self.client.post("/documents/" + "0" * 32 + "/answer", json={"query": "alpha", "allow_external": True}).status_code, 404)
        other = self.upload_and_parse(parse=False)
        self.assertEqual(self.client.post(f"/documents/{other}/answer", json={"query": "alpha", "allow_external": True}).status_code, 409)
        self.assertEqual(self.client.post("/documents/not-an-id/answer", json={"query": "alpha", "allow_external": True}).status_code, 422)
        self.assertEqual(self.requests, [])

    def test_changed_source_before_call_is_rejected_without_spending(self):
        (self.settings.upload_directory / f"{self.document_id}.pdf").write_bytes(b"%PDF-1.7\nchanged")
        self.assertEqual(self.answer().status_code, 409)
        self.assertEqual(self.requests, [])

    def test_changed_source_during_call_discards_already_generated_answer(self):
        def change_source(request):
            (self.settings.upload_directory / f"{self.document_id}.pdf").write_bytes(b"%PDF-1.7\nchanged during call")
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(self.draft)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
        self.install_transport(change_source)
        response = self.answer()
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("测试模型要点", response.text)
        self.assertEqual(len(self.requests), 1)

    def test_answer_generation_does_not_write_source_or_parse_files(self):
        def files():
            return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.settings.data_dir.rglob("*") if path.is_file()}
        before = files()
        self.assertEqual(self.answer().status_code, 200)
        self.assertEqual(before, files())

    def test_request_only_contains_selected_document_question_and_evidence(self):
        self.upload_and_parse(text="omega OTHER-DOCUMENT-MUST-NOT-BE-SENT")
        self.assertEqual(self.answer().status_code, 200)
        raw = self.requests[0].content.decode()
        body = json.loads(raw)
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertNotIn("OTHER-DOCUMENT", raw)
        self.assertNotIn(self.document_id, raw)
        self.assertNotIn(str(self.root), raw)
        user = json.loads(body["messages"][1]["content"])
        self.assertEqual(user["question"], "alpha")
        self.assertEqual(user["evidence"][0]["text"], "alpha beta 第一页")

    def test_openapi_discloses_paid_external_call_and_structural_check_limits(self):
        schema = self.client.get("/openapi.json").json()
        route = schema["paths"]["/documents/{document_id}/answer"]["post"]
        self.assertIn("可能收费", route["summary"])
        self.assertIn("不核验语义", route["summary"])
        self.assertIn("allow_external", schema["components"]["schemas"]["AnswerRequest"]["required"])
        self.assertEqual(schema["components"]["schemas"]["AnswerRequest"]["properties"]["max_context_chars"]["maximum"], 6000)
        self.assertNotIn("offline-fixture-secret", json.dumps(schema))

    def test_explicit_model_settings_override_environment_without_sharing_instances(self):
        with patch.dict("os.environ", {"MATHSCOPE_DEEPSEEK_ENABLED": "invalid"}):
            app = create_app(self.settings, deepseek_settings=DeepSeekSettings())
        with TestClient(app):
            self.assertEqual(app.state.deepseek.call_state()["calls_used"], 0)
            self.assertFalse(app.state.deepseek.settings.enabled)
        self.assertIsNot(app.state.deepseek, self.app.state.deepseek)
        with self.assertRaises(TypeError):
            create_app(self.settings, deepseek_settings={})


if __name__ == "__main__":
    unittest.main()
