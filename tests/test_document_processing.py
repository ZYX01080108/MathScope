import hashlib
import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import api
from src.document_upload import save_pdf_upload
from src.settings import Settings
from src.storage import write_jsonl


class FakePage:
    def __init__(self, text):
        self.text = text

    def extract_text(self):
        return self.text


class FakeReader:
    is_encrypted = False

    def __init__(self, texts):
        self.pages = [FakePage(text) for text in texts]


class DocumentProcessingTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        chunks_path = self.directory / "lecture.chunks.jsonl"
        settings = Settings(data_dir=self.directory, baseline_chunks_path=chunks_path)
        self.uploads = settings.upload_directory
        self.parsed = settings.parsed_directory
        write_jsonl([
            {
                "document_name": "test.pdf", "pdf_page_number": 1,
                "start_char": 0, "end_char": 10, "text": "alpha beta",
            },
        ], chunks_path)
        self.app = api.create_app(settings)
        self.client = self.enterContext(TestClient(self.app))
        self.source = b"%PDF-1.7\nfixture for the mocked reader\n"
        self.metadata = save_pdf_upload(BytesIO(self.source), "讲义.pdf", self.uploads)
        self.document_id = self.metadata["document_id"]

    def parse(self, document_id=None, body=None):
        return self.client.post(
            f"/documents/{document_id or self.document_id}/parse",
            json={"chunk_size": 4} if body is None else body,
        )

    def read_jsonl(self, path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_parse_preserves_empty_page_and_exact_source_offsets(self):
        with patch("src.document_processing.PdfReader", return_value=FakeReader(["ABCDEFGHIJ", "", "XYZ"])):
            response = self.parse()
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertEqual(report["status"], "parsed")
        self.assertEqual(report["page_count"], 3)
        self.assertEqual(report["nonempty_page_count"], 2)
        self.assertEqual(report["empty_page_numbers"], [2])
        self.assertEqual(report["chunk_count"], 4)
        self.assertEqual(report["chunk_size"], 4)
        self.assertEqual(report["source_sha256"], hashlib.sha256(self.source).hexdigest())
        output = self.parsed / self.document_id
        pages = self.read_jsonl(output / "pages.jsonl")
        chunks = self.read_jsonl(output / "chunks.jsonl")
        self.assertEqual([page["pdf_page_number"] for page in pages], [1, 2, 3])
        self.assertEqual(pages[1]["text"], "")
        self.assertEqual([chunk["pdf_page_number"] for chunk in chunks], [1, 1, 1, 3])
        for chunk in chunks:
            self.assertEqual(chunk["document_id"], self.document_id)
            self.assertEqual(chunk["document_name"], "讲义.pdf")
            page = pages[chunk["pdf_page_number"] - 1]
            self.assertEqual(chunk["text"], page["text"][chunk["start_char"]:chunk["end_char"]])
        self.assertEqual(json.loads((output / "parse_report.json").read_text(encoding="utf-8")), report)
        self.assertEqual((self.uploads / f"{self.document_id}.pdf").read_bytes(), self.source)

    def test_unknown_document_is_not_found(self):
        self.assertEqual(self.parse(document_id="a" * 32).status_code, 404)

    def test_invalid_ids_and_chunk_sizes_are_rejected(self):
        self.assertEqual(self.parse(document_id="not-an-id").status_code, 422)
        for size in (0, 10001, "500", True, 1.5):
            with self.subTest(size=size):
                self.assertEqual(self.parse(body={"chunk_size": size}).status_code, 422)

    def test_default_chunk_size(self):
        with patch("src.document_processing.PdfReader", return_value=FakeReader(["alpha"])):
            response = self.parse(body={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["chunk_size"], 500)

    def test_changed_source_is_rejected_before_pdf_parsing(self):
        with (self.uploads / f"{self.document_id}.pdf").open("wb") as output:
            output.write(self.source + b"changed")
        with patch("src.document_processing.PdfReader") as reader:
            self.assertEqual(self.parse().status_code, 409)
            reader.assert_not_called()
        self.assertFalse((self.parsed / self.document_id).exists())

    def test_corrupt_metadata_is_server_error(self):
        with (self.uploads / f"{self.document_id}.json").open("w", encoding="utf-8") as output:
            output.write("not JSON")
        response = self.parse()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(str(self.directory), response.text)

    def test_corrupt_pdf_is_rejected_by_real_reader(self):
        self.assertEqual(self.parse().status_code, 422)
        self.assertFalse((self.parsed / self.document_id).exists())

    def test_encrypted_pdf_is_rejected(self):
        reader = FakeReader(["secret"])
        reader.is_encrypted = True
        with patch("src.document_processing.PdfReader", return_value=reader):
            self.assertEqual(self.parse().status_code, 422)

    def test_page_and_text_limits(self):
        with patch("src.document_processing.PdfReader", return_value=FakeReader(["a", "b"])):
            with patch("src.document_processing.MAX_PARSE_PAGES", 1):
                self.assertEqual(self.parse().status_code, 413)
            with patch("src.document_processing.MAX_EXTRACTED_CHARS", 1):
                self.assertEqual(self.parse().status_code, 413)
        self.assertFalse((self.parsed / self.document_id).exists())

    def test_no_pages_is_rejected(self):
        with patch("src.document_processing.PdfReader", return_value=FakeReader([])):
            self.assertEqual(self.parse().status_code, 422)

    def test_all_empty_pages_produce_warning_not_fake_text(self):
        with patch("src.document_processing.PdfReader", return_value=FakeReader(["", ""])):
            response = self.parse()
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertEqual(report["nonempty_page_count"], 0)
        self.assertEqual(report["chunk_count"], 0)
        self.assertTrue(report["warnings"])

    def test_repeat_parse_does_not_overwrite(self):
        with patch("src.document_processing.PdfReader", return_value=FakeReader(["alpha"])):
            self.assertEqual(self.parse().status_code, 200)
        output = self.parsed / self.document_id
        snapshot = {path.name: path.read_bytes() for path in output.iterdir()}
        with patch("src.document_processing.PdfReader") as reader:
            self.assertEqual(self.parse(body={"chunk_size": 2}).status_code, 409)
            reader.assert_not_called()
        self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, snapshot)

    def test_write_failure_removes_partial_results_but_keeps_source(self):
        real_write_jsonl = write_jsonl

        def fail_chunks(records, path):
            if path.name == "chunks.jsonl":
                raise OSError("private/storage/path")
            return real_write_jsonl(records, path)

        with patch("src.document_processing.PdfReader", return_value=FakeReader(["alpha"])):
            with patch("src.document_processing.write_jsonl", side_effect=fail_chunks):
                response = self.parse()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private/storage", response.text)
        self.assertFalse((self.parsed / self.document_id).exists())
        self.assertEqual((self.uploads / f"{self.document_id}.pdf").read_bytes(), self.source)

    def test_parse_does_not_change_search_index(self):
        before = self.client.post("/search", json={"query": "alpha"}).json()
        with patch("src.document_processing.PdfReader", return_value=FakeReader(["new content"])):
            self.assertEqual(self.parse().status_code, 200)
        self.assertEqual(self.client.post("/search", json={"query": "alpha"}).json(), before)

    def test_parse_response_is_documented(self):
        schema = self.client.get("/openapi.json").json()
        endpoint = schema["paths"]["/documents/{document_id}/parse"]["post"]
        self.assertEqual(
            endpoint["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ParseResponse",
        )


if __name__ == "__main__":
    unittest.main()
