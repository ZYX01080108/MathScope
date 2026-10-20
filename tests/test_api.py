import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.exceptions import ResponseValidationError
from fastapi.testclient import TestClient

from src import api
from src.bm25_retrieval import BM25Retriever
from src.settings import Settings
from src.storage import write_jsonl
from src.tokenization import tokenize, tokenize_normalized


def make_test_chunks():
    texts = [
        "复数域不是一个有序数域。",
        "alpha beta",
        "alpha gamma",
        "omega delta",
    ]
    return [
        {
            "document_name": "test.pdf",
            "pdf_page_number": page_number,
            "start_char": 0,
            "end_char": len(text),
            "text": text,
        }
        for page_number, text in enumerate(texts, start=1)
    ]


class ApiTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = self.enterContext(TemporaryDirectory())
        chunks_path = Path(temporary_directory) / "test.chunks.jsonl"
        self.chunks = make_test_chunks()
        write_jsonl(self.chunks, chunks_path)

        # Give this application its own paths; tokenization and BM25 remain real.
        self.app = api.create_app(Settings(
            data_dir=Path(temporary_directory), baseline_chunks_path=chunks_path,
        ))
        self.client = self.enterContext(TestClient(self.app))

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "ok", "service": "MathScope"},
        )

    def test_search_matches_direct_retrieval_for_both_methods(self):
        tokenizers = {
            "original": tokenize,
            "normalized": tokenize_normalized,
        }
        results_by_method = {}
        for method, tokenizer in tokenizers.items():
            with self.subTest(method=method):
                response = self.client.post(
                    "/search",
                    json={"query": "有序域", "method": method, "k": 2},
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                expected = BM25Retriever(
                    self.chunks, tokenizer=tokenizer,
                ).search("有序域", k=2)

                self.assertTrue(expected)
                self.assertEqual(body["results"], expected)
                self.assertEqual(body["returned_count"], len(expected))
                self.assertEqual(body["query"], "有序域")
                self.assertEqual(body["method"], method)
                self.assertEqual(body["k"], 2)
                self.assertEqual(body["results"][0]["chunk"], self.chunks[0])
                results_by_method[method] = body["results"]

        # The fixture must distinguish the two indexes, not just their labels.
        self.assertNotEqual(
            results_by_method["original"], results_by_method["normalized"],
        )

    def test_defaults_and_query_whitespace(self):
        response = self.client.post("/search", json={"query": "  alpha  "})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["query"], "alpha")
        self.assertEqual(body["method"], "original")
        self.assertEqual(body["k"], 5)
        expected = BM25Retriever(self.chunks).search("alpha", k=5)
        self.assertEqual(body["results"], expected)
        self.assertEqual(body["returned_count"], 2)

    def test_k_limits_returned_results(self):
        response = self.client.post("/search", json={"query": "alpha", "k": 1})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["returned_count"], 1)
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(
            body["results"], BM25Retriever(self.chunks).search("alpha", k=1),
        )

    def test_invalid_inputs_are_rejected(self):
        invalid_inputs = [
            {},
            {"query": ""},
            {"query": " \t\n "},
            {"query": None},
            {"query": 123},
            {"query": []},
            {"query": "a" * 501},
            {"query": "alpha", "k": 0},
            {"query": "alpha", "k": 21},
            {"query": "alpha", "k": "5"},
            {"query": "alpha", "k": 1.5},
            {"query": "alpha", "k": True},
            {"query": "alpha", "method": "vector"},
            {"query": "!!!"},
        ]
        for payload in invalid_inputs:
            with self.subTest(payload=payload):
                response = self.client.post("/search", json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertIn("detail", response.json())

    def test_no_match_returns_empty_results(self):
        response = self.client.post("/search", json={"query": "zzzxqvnotpresent"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["returned_count"], 0)
        self.assertEqual(body["results"], [])

    def test_search_response_schema_is_documented(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        response_schema = schema["paths"]["/search"]["post"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            response_schema["$ref"],
            "#/components/schemas/SearchResponse",
        )
        models = schema["components"]["schemas"]
        self.assertEqual(
            models["SearchResponse"]["properties"]["results"]["items"]["$ref"],
            "#/components/schemas/SearchResult",
        )
        self.assertEqual(
            models["SearchResult"]["properties"]["chunk"]["$ref"],
            "#/components/schemas/EvidenceChunk",
        )

    def test_invalid_search_output_is_rejected(self):
        chunk = self.chunks[0]
        missing_page = {
            key: value for key, value in chunk.items()
            if key != "pdf_page_number"
        }
        invalid_results = [
            {"score": 0.5, "chunk": missing_page},
            {"score": 0.5, "chunk": {**chunk, "pdf_page_number": 0}},
            {"score": 0.5, "chunk": {**chunk, "pdf_page_number": "1"}},
            {"score": 0.5, "chunk": {**chunk, "start_char": -1}},
            {"score": "0.5", "chunk": chunk},
            {"score": float("inf"), "chunk": chunk},
            {"score": float("nan"), "chunk": chunk},
        ]
        retriever = self.app.state.retrievers["original"]
        for result in invalid_results:
            with self.subTest(result=result):
                with patch.object(retriever, "search", return_value=[result]):
                    # TestClient raises server errors by default, exposing bugs.
                    with self.assertRaises(ResponseValidationError):
                        self.client.post("/search", json={"query": "alpha"})

    def test_undeclared_output_fields_are_filtered(self):
        result = {
            "score": 0.5,
            "internal_debug": "not part of the public API",
            "chunk": {**self.chunks[0], "internal_path": "private/test.pdf"},
        }
        retriever = self.app.state.retrievers["original"]
        with patch.object(retriever, "search", return_value=[result]):
            response = self.client.post("/search", json={"query": "alpha"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["results"],
            [{"score": 0.5, "chunk": self.chunks[0]}],
        )

    def test_zero_and_negative_scores_remain_valid(self):
        retriever = self.app.state.retrievers["original"]
        for score in (0.0, -0.5):
            with self.subTest(score=score):
                result = {"score": score, "chunk": self.chunks[0]}
                with patch.object(retriever, "search", return_value=[result]):
                    response = self.client.post("/search", json={"query": "alpha"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["results"], [result])


class ApiLifespanTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = self.enterContext(TemporaryDirectory())
        self.chunks_path = Path(temporary_directory) / "test.chunks.jsonl"
        self.app = api.create_app(Settings(
            data_dir=Path(temporary_directory), baseline_chunks_path=self.chunks_path,
        ))

    def test_missing_baseline_allows_startup_and_returns_503(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/docs").status_code, 200)
            self.assertEqual(client.get("/documents").json()["total"], 0)
            response = client.post("/search", json={"query": "alpha"})
            self.assertEqual(response.status_code, 503)
            self.assertIn("尚未准备", response.json()["detail"])
            self.assertIn("/documents/", response.json()["detail"])
        self.assertFalse(self.chunks_path.exists())

    def test_empty_baseline_allows_startup_and_returns_503(self):
        write_jsonl([], self.chunks_path)
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            response = client.post("/search", json={"query": "alpha"})
            self.assertEqual(response.status_code, 503)
            self.assertIn("分块为空", response.json()["detail"])

    def test_corrupt_baseline_allows_other_services(self):
        with self.chunks_path.open("w", encoding="utf-8") as output:
            output.write("not JSON")
        with self.assertLogs("src.baseline", level="WARNING"):
            with TestClient(self.app) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/documents").status_code, 200)
                response = client.post("/search", json={"query": "alpha"})
                self.assertEqual(response.status_code, 503)
                self.assertIn("检查失败", response.json()["detail"])

    def test_invalid_baseline_records_are_not_silently_used(self):
        for changes in ({"pdf_page_number": "1"}, {"end_char": 999}):
            with self.subTest(changes=changes):
                records = make_test_chunks()
                records[0].update(changes)
                write_jsonl(records, self.chunks_path)
                with self.assertLogs("src.baseline", level="WARNING"):
                    with TestClient(self.app) as client:
                        self.assertEqual(client.get("/health").status_code, 200)
                        self.assertEqual(client.post("/search", json={"query": "alpha"}).status_code, 503)

    def test_baseline_without_searchable_words_is_not_ready(self):
        write_jsonl([
            {"document_name": "test.pdf", "pdf_page_number": 1,
             "start_char": 0, "end_char": 3, "text": "!!!"},
        ], self.chunks_path)
        with TestClient(self.app) as client:
            response = client.post("/search", json={"query": "alpha"})
            self.assertEqual(response.status_code, 503)
            self.assertIn("没有可检索的词", response.json()["detail"])

    def test_new_document_workflow_does_not_need_default_baseline(self):
        for corrupt_baseline in (False, True):
            with self.subTest(corrupt_baseline=corrupt_baseline):
                if corrupt_baseline:
                    with self.chunks_path.open("w", encoding="utf-8") as output:
                        output.write("not JSON")
                log_context = self.assertLogs("src.baseline", level="WARNING") if corrupt_baseline else nullcontext()
                with log_context, TestClient(self.app) as client:
                    uploaded = client.post(
                        "/documents", files={"file": ("own.pdf", b"%PDF-1.7\nfixture", "application/pdf")},
                    )
                    self.assertEqual(uploaded.status_code, 201)
                    document_id = uploaded.json()["document_id"]
                    self.assertEqual(client.get(f"/documents/{document_id}").json()["status"], "uploaded")
                    reader = SimpleNamespace(
                        is_encrypted=False,
                        pages=[SimpleNamespace(extract_text=lambda: "alpha beta")],
                    )
                    with patch("src.document_processing.PdfReader", return_value=reader):
                        parsed = client.post(f"/documents/{document_id}/parse", json={"chunk_size": 500})
                    self.assertEqual(parsed.status_code, 200)
                    self.assertEqual(client.get(f"/documents/{document_id}").json()["status"], "parsed")
                    result = client.post(f"/documents/{document_id}/search", json={"query": "alpha"})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.json()["results"][0]["chunk"]["document_id"], document_id)
                    self.assertEqual(client.post("/search", json={"query": "alpha"}).status_code, 503)

    def test_baseline_prepared_after_startup_requires_restart(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.post("/search", json={"query": "alpha"}).status_code, 503)
            write_jsonl(make_test_chunks(), self.chunks_path)
            self.assertEqual(client.post("/search", json={"query": "alpha"}).status_code, 503)
        with TestClient(self.app) as client:
            self.assertEqual(client.post("/search", json={"query": "alpha"}).status_code, 200)
            self.assertIsNone(self.app.state.baseline_unavailable_reason)

    def test_baseline_unavailable_response_is_documented(self):
        with TestClient(self.app) as client:
            schema = client.get("/openapi.json").json()
            self.assertIn("503", schema["paths"]["/search"]["post"]["responses"])

    def test_baseline_load_limits_do_not_block_startup(self):
        write_jsonl(make_test_chunks(), self.chunks_path)
        for setting in ("MAX_BASELINE_BYTES", "MAX_BASELINE_CHUNKS"):
            with self.subTest(setting=setting):
                with patch(f"src.baseline.{setting}", 1):
                    with self.assertLogs("src.baseline", level="WARNING"):
                        with TestClient(self.app) as client:
                            self.assertEqual(client.get("/health").status_code, 200)
                            self.assertEqual(client.post("/search", json={"query": "alpha"}).status_code, 503)

    def test_shutdown_releases_retrievers(self):
        write_jsonl(make_test_chunks(), self.chunks_path)
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(
                set(self.app.state.retrievers), {"original", "normalized"},
            )
        self.assertEqual(self.app.state.retrievers, {})


if __name__ == "__main__":
    unittest.main()
