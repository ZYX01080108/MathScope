import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import api
from src.document_processing import process_document
from src.document_upload import save_pdf_upload
from src.settings import Settings
from src.storage import write_jsonl


class FakePage:
    def extract_text(self):
        return "alpha"


class FakeReader:
    is_encrypted = False
    pages = [FakePage()]


class DocumentCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        baseline = self.directory / "baseline.jsonl"
        settings = Settings(data_dir=self.directory, baseline_chunks_path=baseline)
        self.uploads = settings.upload_directory
        self.parsed = settings.parsed_directory
        write_jsonl([
            {"document_name": "baseline.pdf", "pdf_page_number": 1,
             "start_char": 0, "end_char": 5, "text": "alpha"},
        ], baseline)
        self.app = api.create_app(settings)
        self.client = self.enterContext(TestClient(self.app))

    def upload(self, parse=False):
        metadata = save_pdf_upload(BytesIO(b"%PDF-1.7\nfixture"), "same-name.pdf", self.uploads)
        document_id = metadata["document_id"]
        if parse:
            with patch("src.document_processing.PdfReader", return_value=FakeReader()):
                process_document(document_id, self.uploads, self.parsed)
        return document_id

    def detail(self, document_id):
        return self.client.get(f"/documents/{document_id}")

    def snapshot_files(self):
        return {str(path.relative_to(self.directory)): path.read_bytes()
                for path in self.directory.rglob("*") if path.is_file()}

    def test_empty_catalog_and_missing_directories(self):
        response = self.client.get("/documents")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {"total": 0, "offset": 0, "limit": 20, "returned_count": 0, "documents": []})
        self.assertFalse(self.uploads.exists())
        self.assertFalse(self.parsed.exists())

    def test_uploaded_document_has_no_invented_parse_counts(self):
        document_id = self.upload()
        response = self.detail(document_id)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "uploaded")
        self.assertEqual(body["document_name"], "same-name.pdf")
        self.assertIsNone(body["page_count"])
        self.assertIsNone(body["chunk_count"])
        self.assertNotIn(str(self.directory), response.text)

    def test_parsed_document_uses_report_not_upload_snapshot_status(self):
        document_id = self.upload(parse=True)
        body = self.detail(document_id).json()
        self.assertEqual(body["status"], "parsed")
        self.assertEqual(body["page_count"], 1)
        self.assertEqual(body["nonempty_page_count"], 1)
        self.assertEqual(body["chunk_count"], 1)
        self.assertEqual(body["chunk_size"], 500)
        uploaded = json.loads((self.uploads / f"{document_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(uploaded["status"], "uploaded")

    def test_same_name_documents_and_stable_pagination(self):
        ids = sorted([self.upload(), self.upload(parse=True), self.upload()])
        response = self.client.get("/documents", params={"offset": 1, "limit": 1})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["returned_count"], 1)
        self.assertEqual(body["documents"][0]["document_id"], ids[1])
        self.assertEqual(self.client.get("/documents", params={"offset": 10}).json()["documents"], [])

    def test_invalid_ids_and_pagination_are_rejected(self):
        self.assertEqual(self.detail("not-an-id").status_code, 422)
        self.assertEqual(self.detail("a" * 32).status_code, 404)
        for params in ({"offset": -1}, {"limit": 0}, {"limit": 101}, {"limit": "bad"}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get("/documents", params=params).status_code, 422)

    def test_incomplete_parse_is_distinct_from_uploaded(self):
        document_id = self.upload()
        directory = self.parsed / document_id
        directory.mkdir(parents=True)
        self.assertEqual(self.detail(document_id).json()["status"], "incomplete")
        document_id = self.upload(parse=True)
        (self.parsed / document_id / "chunks.jsonl").unlink()
        self.assertEqual(self.detail(document_id).json()["status"], "incomplete")

    def test_corrupt_metadata_does_not_hide_other_documents(self):
        valid_id = self.upload()
        bad_id = self.upload()
        with (self.uploads / f"{bad_id}.json").open("w", encoding="utf-8") as output:
            output.write("not JSON")
        response = self.client.get("/documents")
        self.assertEqual(response.status_code, 200)
        records = {record["document_id"]: record for record in response.json()["documents"]}
        self.assertEqual(records[valid_id]["status"], "uploaded")
        self.assertEqual(records[bad_id]["status"], "invalid")
        self.assertIsNone(records[bad_id]["document_name"])

    def test_missing_original_and_orphan_parse_are_visible(self):
        document_id = self.upload(parse=True)
        (self.uploads / f"{document_id}.pdf").unlink()
        self.assertEqual(self.detail(document_id).json()["status"], "invalid")
        (self.uploads / f"{document_id}.json").unlink()
        body = self.client.get("/documents").json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["documents"][0]["status"], "invalid")

    def test_changed_original_is_invalid(self):
        document_id = self.upload(parse=True)
        with (self.uploads / f"{document_id}.pdf").open("ab") as output:
            output.write(b"changed")
        self.assertEqual(self.detail(document_id).json()["status"], "invalid")

    def test_wrong_or_corrupt_parse_report_is_invalid(self):
        document_id = self.upload(parse=True)
        path = self.parsed / document_id / "parse_report.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        report["document_id"] = "a" * 32
        with path.open("w", encoding="utf-8") as output:
            json.dump(report, output)
        self.assertEqual(self.detail(document_id).json()["status"], "invalid")
        with path.open("w", encoding="utf-8") as output:
            output.write("not JSON")
        self.assertEqual(self.detail(document_id).json()["status"], "invalid")

    def test_status_reads_do_not_write_files_or_build_indexes(self):
        document_id = self.upload(parse=True)
        before = self.snapshot_files()
        with patch("src.document_search.BM25Retriever") as builder:
            self.assertEqual(self.client.get("/documents").status_code, 200)
            self.assertEqual(self.detail(document_id).status_code, 200)
            builder.assert_not_called()
        self.assertEqual(self.snapshot_files(), before)

    def test_state_survives_index_cache_clear_and_new_client(self):
        document_id = self.upload(parse=True)
        expected = self.detail(document_id).json()
        self.app.state.document_search.clear()
        with TestClient(self.app) as another_client:
            self.assertEqual(another_client.get(f"/documents/{document_id}").json(), expected)

    def test_get_does_not_replace_post_on_same_path(self):
        uploaded = self.client.post("/documents", files={"file": ("upload.pdf", b"%PDF-1.7\nfixture")})
        self.assertEqual(uploaded.status_code, 201)
        self.assertEqual(self.client.get("/documents").json()["total"], 1)

    def test_catalog_models_are_documented(self):
        schema = self.client.get("/openapi.json").json()
        for path, model in (("/documents", "DocumentListResponse"),
                            ("/documents/{document_id}", "DocumentInfo")):
            with self.subTest(path=path):
                response_schema = schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
                self.assertEqual(response_schema["$ref"], f"#/components/schemas/{model}")


if __name__ == "__main__":
    unittest.main()
