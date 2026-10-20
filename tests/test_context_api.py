import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api import create_app
from src.evidence_context import EvidenceContextError, build_evidence_context
from src.settings import Settings


class ContextApiTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = Settings(data_dir=root / "data")
        self.app = create_app(self.settings)
        self.client = self.enterContext(TestClient(self.app))
        self.document_id = self.prepare(["alpha beta 第一页", "alpha gamma 第二页"])
        self.url = f"/documents/{self.document_id}/context"

    def prepare(self, texts=None):
        upload = self.client.post("/documents", files={"file": ("same.pdf", b"%PDF-1.7\nfixture", "application/pdf")}).json()
        if texts is not None:
            reader = SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda text=text: text) for text in texts])
            with patch("src.document_processing.PdfReader", return_value=reader):
                response = self.client.post(f"/documents/{upload['document_id']}/parse", json={"chunk_size": 500})
            self.assertEqual(response.status_code, 200)
        return upload["document_id"]

    def test_context_matches_direct_retrieval_and_packing(self):
        query = {"query": "alpha", "method": "normalized", "k": 5}
        retrieved = self.client.post(f"/documents/{self.document_id}/search", json=query).json()
        response = self.client.post(self.url, json={**query, "max_context_chars": 6000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), build_evidence_context(retrieved, 6000))
        self.assertEqual(response.json()["status"], "ready")

    def test_defaults_and_query_whitespace_are_documented_and_preserved(self):
        result = self.client.post(self.url, json={"query": "  alpha  "}).json()
        self.assertEqual(result["query"], "alpha")
        self.assertEqual(result["method"], "original")
        self.assertEqual(result["k"], 5)
        self.assertEqual(result["max_context_chars"], 6000)

    def test_budget_status_is_not_confused_with_no_matches(self):
        small = self.client.post(self.url, json={"query": "alpha", "max_context_chars": 128}).json()
        missing = self.client.post(self.url, json={"query": "unmatcheduniquephrase"}).json()
        self.assertEqual(small["status"], "budget_exceeded")
        self.assertGreater(small["retrieved_count"], 0)
        self.assertEqual(missing["status"], "no_matches")
        self.assertEqual(missing["retrieved_count"], 0)

    def test_request_validation_rejects_bad_parameters(self):
        for payload in ({"query": " "}, {"query": "!!!"}, {"query": "alpha", "k": 0},
                        {"query": "alpha", "method": "unknown"}, {"query": "alpha", "max_context_chars": "6000"},
                        {"query": "alpha", "max_context_chars": True}, {"query": "alpha", "max_context_chars": 20_001}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post(self.url, json=payload).status_code, 422)

    def test_missing_and_unparsed_documents_do_not_fall_back_to_baseline(self):
        self.assertEqual(self.client.post("/documents/" + "0" * 32 + "/context", json={"query": "alpha"}).status_code, 404)
        unparsed = self.prepare()
        self.assertEqual(self.client.post(f"/documents/{unparsed}/context", json={"query": "alpha"}).status_code, 409)

    def test_same_name_documents_keep_their_own_evidence(self):
        other = self.prepare(["omega delta"])
        self.assertEqual(self.client.post(self.url, json={"query": "omega"}).json()["status"], "no_matches")
        packet = self.client.post(f"/documents/{other}/context", json={"query": "omega"}).json()
        self.assertEqual(packet["document_id"], other)
        self.assertTrue(all(item["document_id"] == other for item in packet["evidence"]))

    def test_context_reuses_existing_index_cache(self):
        self.client.post(f"/documents/{self.document_id}/search", json={"query": "alpha"})
        with patch("src.document_search.BM25Retriever", side_effect=AssertionError("must reuse cache")):
            response = self.client.post(self.url, json={"query": "alpha"})
        self.assertEqual(response.status_code, 200)

    def test_context_does_not_write_disk_artifacts(self):
        def files():
            return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.settings.data_dir.rglob("*") if path.is_file()}
        before = files()
        self.assertEqual(self.client.post(self.url, json={"query": "alpha"}).status_code, 200)
        self.assertEqual(files(), before)

    def test_changed_source_is_rejected_even_after_cache_warmup(self):
        self.client.post(self.url, json={"query": "alpha"})
        source = self.settings.upload_directory / f"{self.document_id}.pdf"
        source.write_bytes(b"%PDF-1.7\nchanged")
        self.assertEqual(self.client.post(self.url, json={"query": "alpha"}).status_code, 409)

    def test_context_source_can_be_verified_with_original_file_interface(self):
        packet = self.client.post(self.url, json={"query": "alpha"}).json()
        response = self.client.get(f"/documents/{self.document_id}/file", params={"sha256": packet["source_sha256"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), packet["source_sha256"])
        self.assertEqual(len(json.loads(packet["context_text"])), packet["included_count"])

    def test_context_errors_remain_explicit(self):
        with patch("src.api.build_evidence_context", side_effect=EvidenceContextError(409, "source mismatch")):
            response = self.client.post(self.url, json={"query": "alpha"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("source mismatch", response.json()["detail"])

    def test_openapi_explains_budget_and_non_model_response(self):
        schema = self.client.get("/openapi.json").json()
        route = schema["paths"]["/documents/{document_id}/context"]["post"]
        self.assertIn("不调用模型", route["summary"])
        self.assertIn("ContextResponse", route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"])
        self.assertIn("不是模型 token", schema["components"]["schemas"]["ContextRequest"]["properties"]["max_context_chars"]["description"])


if __name__ == "__main__":
    unittest.main()
