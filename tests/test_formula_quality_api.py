import hashlib
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from src.api import create_app
from src.deepseek_client import DeepSeekSettings
from src.settings import Settings


class FormulaQualityApiTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = Settings(data_dir=root / "data")
        self.app = create_app(self.settings, deepseek_settings=DeepSeekSettings())
        self.client = self.enterContext(TestClient(self.app))
        self.forbidden_model = self.enterContext(patch.object(self.app.state.deepseek, "complete",
                                                           side_effect=AssertionError("must not call a paid model")))
        self.document_id = self.prepare()
        self.query = {"query": "alpha", "method": "normalized", "k": 10, "max_context_chars": 6000}
        self.url = f"/documents/{self.document_id}"

    def prepare(self, content=b"q 0.478 w 166.99 200 m 175.68 200 l S Q", parse=True):
        writer = PdfWriter()
        for number in range(9):
            page = writer.add_blank_page(width=612, height=792)
            if number == 8 and content:
                stream = DecodedStreamObject()
                stream.set_data(content)
                page[NameObject("/Contents")] = writer._add_object(stream)
        output = BytesIO()
        writer.write(output)
        upload = self.client.post("/documents", files={"file": ("fixture.pdf", output.getvalue(), "application/pdf")}).json()
        document_id = upload["document_id"]
        if parse:
            pages = [SimpleNamespace(extract_text=lambda: "") for _ in range(8)]
            pages.append(SimpleNamespace(extract_text=lambda: "alpha 不等式成为等式的充要条件是 zw ≥ 0."))
            with patch("src.document_processing.PdfReader", return_value=SimpleNamespace(is_encrypted=False, pages=pages)):
                response = self.client.post(f"/documents/{document_id}/parse", json={"chunk_size": 500})
            self.assertEqual(response.status_code, 200)
        return document_id

    def test_quality_endpoint_is_read_only_and_bound_to_context_source_and_pages(self):
        response = self.client.post(self.url + "/quality", json=self.query)
        self.assertEqual(response.status_code, 200)
        quality = response.json()
        context = self.client.post(self.url + "/context", json=self.query).json()
        self.assertEqual(quality["document_id"], self.document_id)
        self.assertEqual(quality["source_sha256"], context["source_sha256"])
        self.assertEqual(quality["context_id"], context["context_id"])
        self.assertEqual(quality["status"], "risk_detected")
        self.assertEqual(quality["checked_page_numbers"], [9])
        self.assertTrue(quality["blocks_generation"])
        self.assertFalse(quality["text_modified"])
        self.forbidden_model.assert_not_called()

    def test_known_formula_risk_refuses_before_any_model_attempt(self):
        response = self.client.post(self.url + "/answer", json={**self.query, "allow_external": True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "refused")
        self.assertFalse(body["generation"]["model_called"])
        self.assertEqual(body["generation"]["calls_used"], 0)
        self.assertIsNone(body["generation"]["usage"])
        self.assertEqual(body["check"]["claims"], [])
        self.assertIn("物理页 [9]", body["check"]["refusal_reason"])
        self.assertFalse(body["check"]["semantics_verified"])
        self.forbidden_model.assert_not_called()

    def test_quality_check_does_not_rewrite_the_original_extracted_text(self):
        before = self.client.post(self.url + "/context", json=self.query).json()
        self.client.post(self.url + "/quality", json=self.query)
        self.client.post(self.url + "/answer", json={**self.query, "allow_external": True})
        after = self.client.post(self.url + "/context", json=self.query).json()
        self.assertEqual(before, after)
        self.assertIn("zw ≥ 0", after["evidence"][0]["text"])
        self.assertNotIn("conjugate", after["evidence"][0]["text"])

    def test_no_flags_does_not_claim_formulas_or_mathematics_are_certified(self):
        document_id = self.prepare(content=b"")
        quality = self.client.post(f"/documents/{document_id}/quality", json=self.query).json()
        self.assertEqual(quality["status"], "not_flagged")
        self.assertFalse(quality["blocks_generation"])
        self.assertIn("不代表公式完整或数学正确", quality["pages"][0]["detail"])

    def test_unsupported_transformation_blocks_generation_as_unassessed(self):
        document_id = self.prepare(content=b"2 0 0 2 0 0 cm")
        response = self.client.post(f"/documents/{document_id}/answer", json={**self.query, "allow_external": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"]["status"], "unassessed")
        self.assertEqual(response.json()["status"], "refused")
        self.forbidden_model.assert_not_called()

    def test_missing_matches_and_tiny_budget_do_not_scan_or_call_model(self):
        for changes in ({"query": "nomatchinguniquephrase"}, {"max_context_chars": 128}):
            with patch("src.formula_quality.get_document_file", side_effect=AssertionError("no evidence must skip PDF scan")):
                quality = self.client.post(self.url + "/quality", json={**self.query, **changes}).json()
                answer = self.client.post(self.url + "/answer", json={**self.query, **changes, "allow_external": True}).json()
            self.assertEqual(quality["status"], "no_evidence")
            self.assertFalse(answer["generation"]["model_called"])
        self.forbidden_model.assert_not_called()

    def test_bad_quality_request_and_client_supplied_report_are_rejected(self):
        for changes in ({"query": " "}, {"k": 0}, {"max_context_chars": 6001}, {"allow_external": True}):
            self.assertEqual(self.client.post(self.url + "/quality", json={**self.query, **changes}).status_code, 422)
        body = {**self.query, "allow_external": True, "quality": {"status": "not_flagged"}}
        self.assertEqual(self.client.post(self.url + "/answer", json=body).status_code, 422)
        self.forbidden_model.assert_not_called()

    def test_missing_unparsed_and_changed_source_fail_before_quality_or_model(self):
        self.assertEqual(self.client.post("/documents/" + "0" * 32 + "/quality", json=self.query).status_code, 404)
        unparsed = self.prepare(parse=False)
        self.assertEqual(self.client.post(f"/documents/{unparsed}/quality", json=self.query).status_code, 409)
        source = self.settings.upload_directory / f"{self.document_id}.pdf"
        source.write_bytes(b"%PDF-1.7\nchanged")
        self.assertEqual(self.client.post(self.url + "/quality", json=self.query).status_code, 409)
        self.assertEqual(self.client.post(self.url + "/answer", json={**self.query, "allow_external": True}).status_code, 409)
        self.forbidden_model.assert_not_called()

    def test_quality_read_failures_do_not_leak_local_paths_or_call_model(self):
        with patch("src.api.assess_document_formula_quality", side_effect=OSError("private path")):
            for endpoint, payload in (("/quality", self.query), ("/answer", {**self.query, "allow_external": True})):
                response = self.client.post(self.url + endpoint, json=payload)
                self.assertEqual(response.status_code, 500)
                self.assertNotIn("private path", response.text)
        self.forbidden_model.assert_not_called()

    def test_both_quality_and_blocked_answer_preserve_all_document_files(self):
        def files():
            return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.settings.data_dir.rglob("*") if path.is_file()}
        before = files()
        self.client.post(self.url + "/quality", json=self.query)
        self.client.post(self.url + "/answer", json={**self.query, "allow_external": True})
        self.assertEqual(before, files())

    def test_openapi_discloses_heuristic_and_no_model_call(self):
        schema = self.client.get("/openapi.json").json()
        summary = schema["paths"]["/documents/{document_id}/quality"]["post"]["summary"]
        self.assertIn("不恢复公式", summary)
        self.assertIn("不调用模型", summary)
        self.assertIn("FormulaQualityResponse", schema["paths"]["/documents/{document_id}/quality"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"])


if __name__ == "__main__":
    unittest.main()
