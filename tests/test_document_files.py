import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api import create_app
from src.document_files import get_document_file
from src.settings import Settings


class DocumentFileTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.settings = Settings(data_dir=Path(temporary) / "data")
        self.app = create_app(self.settings)
        self.client = self.enterContext(TestClient(self.app))
        self.content = b"%PDF-1.7\ntransport-test-fixture"
        response = self.client.post("/documents", files={"file": ("数学讲义.pdf", self.content, "application/pdf")})
        self.assertEqual(response.status_code, 201)
        self.record = response.json()
        self.document_id = self.record["document_id"]
        self.pdf = self.settings.upload_directory / f"{self.document_id}.pdf"
        self.metadata = self.settings.upload_directory / f"{self.document_id}.json"
        self.url = f"/documents/{self.document_id}/file"

    def write_metadata(self, **changes):
        self.metadata.write_text(json.dumps({**self.record, **changes}), encoding="utf-8")

    def test_returns_exact_original_bytes_without_parsing_or_building_index(self):
        before = self.metadata.read_bytes()
        response = self.client.get(self.url, params={"sha256": self.record["sha256"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.content)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["content-disposition"], f'inline; filename="{self.document_id}.pdf"')
        self.assertEqual(response.headers["content-length"], str(len(self.content)))
        self.assertEqual(response.headers["etag"], f'"{self.record["sha256"]}"')
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(before, self.metadata.read_bytes())
        self.assertFalse(self.settings.parsed_directory.exists())
        self.assertFalse(self.app.state.document_search._cache)

    def test_unknown_document_is_404(self):
        self.assertEqual(self.client.get("/documents/" + "0" * 32 + "/file").status_code, 404)

    def test_invalid_identifiers_and_fingerprints_are_rejected(self):
        self.assertEqual(self.client.get("/documents/not-an-id/file").status_code, 422)
        for value in ("", "a" * 63, "A" * 64, "../file", "z" * 64):
            with self.subTest(value=value):
                self.assertEqual(self.client.get(self.url, params={"sha256": value}).status_code, 422)

    def test_missing_upload_record_does_not_expose_pdf(self):
        self.metadata.unlink()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_missing_source_is_404(self):
        self.pdf.unlink()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_corrupt_metadata_is_409(self):
        self.metadata.write_text("not json", encoding="utf-8")
        self.assertEqual(self.client.get(self.url).status_code, 409)

    def test_metadata_document_id_must_match(self):
        self.write_metadata(document_id="0" * 32)
        self.assertEqual(self.client.get(self.url).status_code, 409)

    def test_same_size_source_change_is_rejected(self):
        self.pdf.write_bytes(self.content[:-1] + b"X")
        self.assertEqual(self.client.get(self.url).status_code, 409)

    def test_size_mismatch_is_rejected(self):
        self.write_metadata(size_bytes=len(self.content) + 1)
        self.assertEqual(self.client.get(self.url).status_code, 409)

    def test_expected_evidence_fingerprint_is_enforced(self):
        self.assertEqual(self.client.get(self.url, params={"sha256": "0" * 64}).status_code, 409)

    def test_replaced_source_and_record_cannot_satisfy_old_evidence_link(self):
        updated = self.content + b"\nchanged"
        self.pdf.write_bytes(updated)
        self.write_metadata(size_bytes=len(updated), sha256=hashlib.sha256(updated).hexdigest())
        self.assertEqual(self.client.get(self.url).content, updated)
        response = self.client.get(self.url, params={"sha256": self.record["sha256"]})
        self.assertEqual(response.status_code, 409)

    def test_non_pdf_header_is_rejected_even_with_matching_record(self):
        changed = b"not a pdf"
        self.pdf.write_bytes(changed)
        self.write_metadata(size_bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest())
        self.assertEqual(self.client.get(self.url).status_code, 409)

    def test_pdf_read_limit_is_enforced(self):
        with patch("src.document_files.MAX_UPLOAD_BYTES", 8):
            self.assertEqual(self.client.get(self.url).status_code, 413)

    def test_metadata_read_limit_is_enforced(self):
        with patch("src.document_files.MAX_METADATA_BYTES", 4):
            self.assertEqual(self.client.get(self.url).status_code, 409)

    def test_symlink_guard_is_checked_before_source_read(self):
        with patch.object(Path, "is_symlink", lambda path: path.suffix == ".pdf"):
            self.assertEqual(self.client.get(self.url).status_code, 409)

    def test_resolved_source_parent_must_remain_in_upload_directory(self):
        original = Path.resolve
        def redirected(path, *args, **kwargs):
            if path == self.pdf:
                return self.settings.data_dir / "outside" / path.name
            return original(path, *args, **kwargs)
        with patch.object(Path, "resolve", redirected):
            self.assertEqual(self.client.get(self.url).status_code, 409)

    def test_response_serves_checked_snapshot_not_reopened_file(self):
        def replaced_after_read(*args, **kwargs):
            snapshot = get_document_file(*args, **kwargs)
            self.pdf.write_bytes(b"%PDF-1.7\nreplacement-after-check")
            return snapshot
        with patch("src.api.get_document_file", side_effect=replaced_after_read):
            self.assertEqual(self.client.get(self.url).content, self.content)
        self.assertEqual(self.client.get(self.url).status_code, 409)

    def test_read_error_is_generic_and_does_not_expose_server_paths(self):
        with patch("src.api.get_document_file", side_effect=PermissionError("private server path")):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private server path", response.text)

    def test_range_request_returns_full_validated_snapshot(self):
        response = self.client.get(self.url, headers={"Range": "bytes=0-4"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.content)
        self.assertEqual(response.headers["accept-ranges"], "none")

    def test_openapi_documents_binary_pdf_and_optional_fingerprint(self):
        operation = self.client.get("/openapi.json").json()["paths"]["/documents/{document_id}/file"]["get"]
        self.assertIn("application/pdf", operation["responses"]["200"]["content"])
        fingerprint = next(item for item in operation["parameters"] if item["name"] == "sha256")
        self.assertFalse(fingerprint["required"])


if __name__ == "__main__":
    unittest.main()
