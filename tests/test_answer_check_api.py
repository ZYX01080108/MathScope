import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api import create_app
from src.settings import Settings


class AnswerCheckApiTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = Settings(data_dir=root / "data")
        self.client = self.enterContext(TestClient(create_app(self.settings)))
        upload = self.client.post("/documents", files={"file": ("test.pdf", b"%PDF-1.7\nfixture", "application/pdf")}).json()
        self.document_id = upload["document_id"]
        reader = SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: "alpha beta")])
        with patch("src.document_processing.PdfReader", return_value=reader):
            parsed = self.client.post(f"/documents/{self.document_id}/parse", json={"chunk_size": 500})
        self.assertEqual(parsed.status_code, 200)
        self.root_url = f"/documents/{self.document_id}"
        self.query = {"query": "alpha", "method": "normalized", "k": 5, "max_context_chars": 6000}
        self.packet = self.client.post(self.root_url + "/context", json=self.query).json()
        self.draft = {"decision": "answer", "claims": [{"text": "测试草稿", "citations": [{"citation_id": "E1", "quote": "alpha"}]}]}

    def check(self, **changes):
        payload = {**self.query, "context_id": self.packet["context_id"], "draft": self.draft, **changes}
        return self.client.post(self.root_url + "/check-answer", json=payload)

    def test_valid_draft_returns_checked_sources_not_a_model_answer(self):
        response = self.check()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "passed")
        self.assertEqual(body["context_id"], self.packet["context_id"])
        self.assertFalse(body["semantics_verified"])
        self.assertEqual(body["claims"][0]["citations"][0]["document_id"], self.document_id)

    def test_unknown_reference_returns_200_rejection_not_accepted_answer(self):
        self.draft["claims"][0]["citations"][0]["citation_id"] = "E99"
        response = self.check()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "rejected")
        self.assertEqual(response.json()["claims"], [])

    def test_wrong_quote_is_explicitly_rejected(self):
        self.draft["claims"][0]["citations"][0]["quote"] = "not in this evidence"
        body = self.check().json()
        self.assertEqual(body["issues"][0]["code"], "quote_not_found")

    def test_changed_query_method_k_or_budget_requires_new_context(self):
        for changes in ({"query": "beta"}, {"method": "original"}, {"k": 6}, {"max_context_chars": 6001}):
            with self.subTest(changes=changes):
                self.assertEqual(self.check(**changes).status_code, 409)

    def test_wrong_context_id_is_409(self):
        self.assertEqual(self.check(context_id="0" * 64).status_code, 409)

    def test_invalid_body_or_model_supplied_source_fields_are_422(self):
        extra_source = {"decision": "answer", "claims": [{"text": "test", "citations": [{"citation_id": "E1", "quote": "alpha", "pdf_page_number": 99}]}]}
        for changes in ({"context_id": "bad"}, {"draft": {"decision": "unknown"}}, {"draft": extra_source}, {"context": self.packet}):
            with self.subTest(changes=changes):
                self.assertEqual(self.check(**changes).status_code, 422)

    def test_changed_source_is_rejected_even_with_matching_old_context_id(self):
        source = self.settings.upload_directory / f"{self.document_id}.pdf"
        source.write_bytes(b"%PDF-1.7\nchanged")
        self.assertEqual(self.check().status_code, 409)

    def test_source_references_are_server_generated_and_file_verified(self):
        body = self.check().json()
        response = self.client.get(self.root_url + "/file", params={"sha256": body["source_sha256"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), body["source_sha256"])

    def test_refusal_format_can_be_checked_without_automatic_refusal_judgment(self):
        body = self.check(draft={"decision": "refuse", "reason": "证据不足"}).json()
        self.assertEqual(body["status"], "passed")
        self.assertEqual(body["refusal_reason"], "证据不足")
        self.assertFalse(body["semantics_verified"])

    def test_no_document_writes_occur(self):
        def fingerprints():
            return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.settings.data_dir.rglob("*") if path.is_file()}
        before = fingerprints()
        self.assertEqual(self.check().status_code, 200)
        self.assertEqual(fingerprints(), before)

    def test_missing_document_and_read_errors_are_explicit(self):
        body = {**self.query, "context_id": self.packet["context_id"], "draft": self.draft}
        response = self.client.post("/documents/" + "0" * 32 + "/check-answer", json=body)
        self.assertEqual(response.status_code, 404)
        with patch("src.api.build_evidence_context", side_effect=OSError("private path")):
            response = self.check()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private path", response.text)

    def test_openapi_describes_structural_not_semantic_validation(self):
        schema = self.client.get("/openapi.json").json()
        summary = schema["paths"]["/documents/{document_id}/check-answer"]["post"]["summary"]
        self.assertIn("不核验语义", summary)
        response_fields = schema["components"]["schemas"]["AnswerCheckResponse"]["properties"]
        self.assertEqual(response_fields["semantics_verified"]["const"], False)


if __name__ == "__main__":
    unittest.main()
