import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import api
from src.bm25_retrieval import BM25Retriever
from src.document_processing import process_document
from src.document_search import DocumentSearchService
from src.document_upload import save_pdf_upload
from src.settings import Settings
from src.storage import write_jsonl
from src.tokenization import tokenize, tokenize_normalized


class FakePage:
    def __init__(self, text):
        self.text = text

    def extract_text(self):
        return self.text


class FakeReader:
    is_encrypted = False

    def __init__(self, texts):
        self.pages = [FakePage(text) for text in texts]


class DocumentSearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        baseline = self.directory / "lecture.chunks.jsonl"
        settings = Settings(data_dir=self.directory, baseline_chunks_path=baseline)
        self.uploads = settings.upload_directory
        self.parsed = settings.parsed_directory
        write_jsonl([
            {"document_name": "baseline.pdf", "pdf_page_number": 1,
             "start_char": 0, "end_char": 14, "text": "baseline alpha"},
        ], baseline)
        self.app = api.create_app(settings)
        self.client = self.enterContext(TestClient(self.app))
        self.first = self.prepare_document(["复数域不是一个有序数域。", "alpha beta", "alpha gamma", ""])
        self.second = self.prepare_document(["omega delta", "second exclusive"])

    def prepare_document(self, texts, parse=True):
        metadata = save_pdf_upload(BytesIO(b"%PDF-1.7\nfixture"), "same-name.pdf", self.uploads)
        if parse:
            with patch("src.document_processing.PdfReader", return_value=FakeReader(texts)):
                process_document(metadata["document_id"], self.uploads, self.parsed)
        return metadata["document_id"]

    def search(self, document_id=None, payload=None):
        return self.client.post(
            f"/documents/{document_id or self.first}/search",
            json={"query": "alpha", "k": 5} if payload is None else payload,
        )

    def chunks(self, document_id):
        path = self.parsed / document_id / "chunks.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_results_match_direct_bm25_for_both_methods(self):
        actual = {}
        for method, tokenizer in (("original", tokenize), ("normalized", tokenize_normalized)):
            with self.subTest(method=method):
                response = self.search(payload={"query": "有序域", "method": method, "k": 5})
                self.assertEqual(response.status_code, 200)
                body = response.json()
                expected = BM25Retriever(self.chunks(self.first), tokenizer=tokenizer).search("有序域", k=5)
                self.assertTrue(expected)
                self.assertEqual(body["results"], expected)
                self.assertEqual(body["document_id"], self.first)
                self.assertEqual(body["indexed_chunk_count"], 3)
                self.assertEqual(body["returned_count"], len(expected))
                self.assertEqual(body["results"][0]["chunk"]["document_id"], self.first)
                actual[method] = body["results"]
        self.assertNotEqual(actual["original"], actual["normalized"])

    def test_same_filename_documents_remain_isolated(self):
        self.assertEqual(self.search(self.first, {"query": "omega"}).json()["results"], [])
        second = self.search(self.second, {"query": "omega"}).json()
        self.assertEqual(second["returned_count"], 1)
        self.assertEqual(second["results"][0]["chunk"]["document_id"], self.second)

    def test_cache_reuses_index_for_different_queries(self):
        with patch("src.document_search.BM25Retriever", wraps=BM25Retriever) as builder:
            first = self.search().json()
            second = self.search(payload={"query": "beta"}).json()
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(builder.call_count, 1)

    def test_methods_use_separate_cached_indexes(self):
        self.assertFalse(self.search().json()["cache_hit"])
        self.assertFalse(self.search(payload={"query": "alpha", "method": "normalized"}).json()["cache_hit"])
        self.assertTrue(self.search().json()["cache_hit"])

    def test_cache_evicts_least_recently_used_index(self):
        service = DocumentSearchService(self.uploads, self.parsed, max_cached_indexes=1)
        with patch("src.document_search.BM25Retriever", wraps=BM25Retriever) as builder:
            service.search(self.first, "alpha")
            service.search(self.second, "omega")
            result = service.search(self.first, "alpha")
        self.assertFalse(result["cache_hit"])
        self.assertEqual(builder.call_count, 3)

    def test_changed_file_bytes_rebuild_equivalent_valid_index(self):
        self.assertFalse(self.search().json()["cache_hit"])
        path = self.parsed / self.first / "chunks.jsonl"
        with path.open("ab") as output:
            output.write(b"\n")
        self.assertFalse(self.search().json()["cache_hit"])
        self.assertTrue(self.search().json()["cache_hit"])

    def test_changed_original_is_rejected_even_with_cached_index(self):
        self.assertEqual(self.search().status_code, 200)
        path = self.uploads / f"{self.first}.pdf"
        with path.open("ab") as output:
            output.write(b"changed")
        self.assertEqual(self.search().status_code, 409)

    def test_corrupt_chunks_are_rejected_even_with_cached_index(self):
        self.assertEqual(self.search().status_code, 200)
        chunks = self.chunks(self.first)
        chunks[0]["text"] = "tampered"
        write_jsonl(chunks, self.parsed / self.first / "chunks.jsonl")
        self.assertEqual(self.search().status_code, 409)

    def test_wrong_document_id_or_offsets_are_rejected(self):
        path = self.parsed / self.first / "chunks.jsonl"
        original = self.chunks(self.first)
        for changes in ({"document_id": self.second}, {"start_char": 1}, {"pdf_page_number": True}):
            with self.subTest(changes=changes):
                altered = [dict(chunk) for chunk in original]
                altered[0].update(changes)
                write_jsonl(altered, path)
                self.assertEqual(self.search().status_code, 409)

    def test_incomplete_or_corrupt_reports_are_rejected(self):
        path = self.parsed / self.first / "parse_report.json"
        with path.open("w", encoding="utf-8") as output:
            output.write("not JSON")
        self.assertEqual(self.search().status_code, 409)
        path.unlink()
        self.assertEqual(self.search().status_code, 409)

    def test_unknown_and_unparsed_documents_do_not_fall_back(self):
        self.assertEqual(self.search("a" * 32).status_code, 404)
        unparsed = self.prepare_document([], parse=False)
        self.assertEqual(self.search(unparsed).status_code, 409)

    def test_request_validation_and_no_match(self):
        for payload in ({"query": "   "}, {"query": "!!!"}, {"query": "alpha", "k": 0},
                        {"query": "alpha", "method": "vector"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.search(payload=payload).status_code, 422)
        self.assertEqual(self.search(document_id="bad-id").status_code, 422)
        body = self.search(payload={"query": "zzzxqvnotpresent"}).json()
        self.assertEqual(body["returned_count"], 0)
        self.assertEqual(body["results"], [])

    def test_defaults_whitespace_and_k_limit(self):
        response = self.search(payload={"query": "  alpha  ", "k": 1})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["query"], "alpha")
        self.assertEqual(body["method"], "original")
        self.assertEqual(body["returned_count"], 1)

    def test_all_empty_document_has_no_fabricated_evidence(self):
        empty = self.prepare_document(["", ""])
        response = self.search(empty)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["indexed_chunk_count"], 0)
        self.assertEqual(response.json()["results"], [])

    def test_baseline_endpoint_and_shutdown_are_unchanged(self):
        before = self.client.post("/search", json={"query": "alpha"}).json()
        self.assertEqual(self.search().status_code, 200)
        self.assertEqual(self.client.post("/search", json={"query": "alpha"}).json(), before)
        service = self.app.state.document_search
        service.clear()
        self.assertFalse(self.search().json()["cache_hit"])

    def test_concurrent_requests_build_only_one_index(self):
        service = self.app.state.document_search
        with patch("src.document_search.BM25Retriever", wraps=BM25Retriever) as builder:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: service.search(self.first, "alpha"), range(2)))
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(sorted(result["cache_hit"] for result in results), [False, True])

    def test_search_response_is_documented(self):
        schema = self.client.get("/openapi.json").json()
        endpoint = schema["paths"]["/documents/{document_id}/search"]["post"]
        self.assertEqual(
            endpoint["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DocumentSearchResponse",
        )


if __name__ == "__main__":
    unittest.main()
