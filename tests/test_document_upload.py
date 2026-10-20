import hashlib
import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import api
from src.document_upload import save_pdf_upload
from src.settings import Settings
from src.storage import write_jsonl


# Header-shaped bytes deliberately avoid relying on a private real PDF.
# Saving these bytes must not be confused with successful PDF parsing.
PDF_BYTES = b"%PDF-1.7\ntest upload bytes\n"


class DocumentUploadTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        chunks_path = self.directory / "lecture.chunks.jsonl"
        settings = Settings(data_dir=self.directory, baseline_chunks_path=chunks_path)
        self.upload_directory = settings.upload_directory
        write_jsonl([
            {
                "document_name": "test.pdf",
                "pdf_page_number": 1,
                "start_char": 0,
                "end_char": 10,
                "text": "alpha beta",
            },
        ], chunks_path)
        self.app = api.create_app(settings)
        self.client = self.enterContext(TestClient(self.app))

    def upload(self, filename="讲义.pdf", content=PDF_BYTES, content_type="application/pdf"):
        return self.client.post(
            "/documents", files={"file": (filename, content, content_type)},
        )

    def assert_no_uploads(self):
        if self.upload_directory.exists():
            self.assertEqual(list(self.upload_directory.iterdir()), [])

    def test_upload_saves_bytes_and_metadata(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertRegex(body["document_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(body["document_name"], "讲义.pdf")
        self.assertEqual(body["status"], "uploaded")
        self.assertEqual(body["size_bytes"], len(PDF_BYTES))
        self.assertEqual(body["sha256"], hashlib.sha256(PDF_BYTES).hexdigest())
        pdf_path = self.upload_directory / f"{body['document_id']}.pdf"
        metadata_path = self.upload_directory / f"{body['document_id']}.json"
        self.assertEqual(pdf_path.read_bytes(), PDF_BYTES)
        self.assertEqual(json.loads(metadata_path.read_text(encoding="utf-8")), body)
        self.assertNotIn("path", body)

    def test_missing_file_is_rejected(self):
        self.assertEqual(self.client.post("/documents").status_code, 422)
        self.assert_no_uploads()

    def test_wrong_extension_is_rejected(self):
        self.assertEqual(self.upload(filename="notes.txt").status_code, 415)
        self.assert_no_uploads()

    def test_renamed_non_pdf_is_rejected(self):
        self.assertEqual(self.upload(content=b"not a PDF").status_code, 415)
        self.assert_no_uploads()

    def test_empty_file_is_rejected(self):
        self.assertEqual(self.upload(content=b"").status_code, 400)
        self.assert_no_uploads()

    def test_oversized_file_is_rejected(self):
        with patch.object(api, "MAX_UPLOAD_BYTES", 32):
            response = self.upload(content=b"%PDF-" + b"x" * 28)
        self.assertEqual(response.status_code, 413)
        self.assert_no_uploads()

    def test_exact_size_limit_and_uppercase_extension_are_allowed(self):
        content = b"%PDF-" + b"x" * 27
        with patch.object(api, "MAX_UPLOAD_BYTES", 32):
            response = self.upload(filename="notes.PDF", content=content)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["size_bytes"], 32)

    def test_duplicate_names_do_not_overwrite(self):
        first = self.upload().json()
        second_content = PDF_BYTES + b"second"
        response = self.upload(content=second_content)
        self.assertEqual(response.status_code, 201)
        second = response.json()
        self.assertNotEqual(first["document_id"], second["document_id"])
        for metadata, content in ((first, PDF_BYTES), (second, second_content)):
            stored = self.upload_directory / f"{metadata['document_id']}.pdf"
            self.assertEqual(stored.read_bytes(), content)

    def test_client_paths_are_never_used_for_storage(self):
        for filename in ("../../escape.pdf", r"C:\private\escape.pdf"):
            with self.subTest(filename=filename):
                response = self.upload(filename=filename)
                self.assertEqual(response.status_code, 201)
                body = response.json()
                self.assertEqual(body["document_name"], "escape.pdf")
                stored = self.upload_directory / f"{body['document_id']}.pdf"
                self.assertTrue(stored.exists())
        self.assertFalse((self.directory / "escape.pdf").exists())

    def test_storage_failure_cleans_partial_files_and_hides_paths(self):
        original_open = Path.open

        def fail_metadata(path, *args, **kwargs):
            if path.parent == self.upload_directory and path.suffix == ".json":
                raise OSError("private/server/storage/path")
            return original_open(path, *args, **kwargs)

        with patch("src.document_upload.Path.open", new=fail_metadata):
            response = self.upload()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private/server", response.text)
        self.assert_no_uploads()

    def test_upload_is_documented_as_multipart(self):
        schema = self.client.get("/openapi.json").json()
        endpoint = schema["paths"]["/documents"]["post"]
        self.assertIn("multipart/form-data", endpoint["requestBody"]["content"])
        self.assertEqual(
            endpoint["responses"]["201"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/UploadResponse",
        )

    def test_upload_does_not_change_search_index(self):
        before = self.client.post("/search", json={"query": "alpha"}).json()
        self.assertEqual(self.upload().status_code, 201)
        after = self.client.post("/search", json={"query": "alpha"}).json()
        self.assertEqual(before, after)

    def test_stream_size_check_cleans_partially_written_file(self):
        content = b"%PDF-" + b"x" * (64 * 1024)
        with self.assertRaisesRegex(ValueError, "超过上传大小上限"):
            save_pdf_upload(
                BytesIO(content), "notes.pdf", self.upload_directory,
                max_bytes=64 * 1024,
            )
        self.assert_no_uploads()

    def test_identifier_collision_does_not_delete_existing_file(self):
        self.upload_directory.mkdir(parents=True)
        document_id = "a" * 32
        existing = self.upload_directory / f"{document_id}.pdf"
        with existing.open("xb") as output:
            output.write(b"existing file")
        with patch("src.document_upload.uuid4", return_value=SimpleNamespace(hex=document_id)):
            with self.assertRaises(FileExistsError):
                save_pdf_upload(BytesIO(PDF_BYTES), "notes.pdf", self.upload_directory)
        self.assertEqual(existing.read_bytes(), b"existing file")


if __name__ == "__main__":
    unittest.main()
