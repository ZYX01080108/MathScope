import copy
import hashlib
import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from src.api import create_app
from src.answer_check import AnswerCheckError
from src.deepseek_client import DeepSeekClient, DeepSeekSettings
from src.reviewed_formula import (
    MAX_REGISTRY_BYTES, build_reviewed_context, check_reviewed_draft,
    generate_reviewed_answer, load_formula_reviews, validate_reviewed_context,
)
from src.settings import Settings


class ReviewFixture:
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = Settings(data_dir=self.root / "data")
        self.app = create_app(self.settings, deepseek_settings=DeepSeekSettings())
        self.client = self.enterContext(TestClient(self.app))
        self.document_id, self.source_text = self.prepare()
        self.url = f"/documents/{self.document_id}"
        self.query = {"query": "alpha", "method": "normalized", "k": 10, "max_context_chars": 6000}
        self.raw = self.client.post(self.url + "/context", json=self.query).json()
        self.record = {
            "review_id": "FR001", "source_sha256": self.raw["source_sha256"], "pdf_page_number": 9,
            "raw_start_char": 0, "raw_end_char": len(self.source_text), "raw_quote": self.source_text,
            "reviewed_statement": "Equality in |z+w| <= |z|+|w| holds iff z*conj(w) is nonnegative real.",
            "formula_latex": r"z\overline{w}\in[0,\infty)", "review_method": "assistant_visual_review",
            "reviewed_at": "2026-10-15", "scope": "single_statement",
        }
        self.registry_path = self.root / "reviews.json"
        self.write_records([self.record])
        self.enterContext(patch("src.reviewed_formula.REVIEW_REGISTRY", self.registry_path))
        self.requests = []

    def prepare(self, text="alpha zw >= 0.", bar=True):
        writer = PdfWriter()
        for _ in range(9):
            writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page = writer.pages[8]
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = (b"q 0.478 w 100 650 m 108 650 l S Q\n" if bar else b"")
        content += b"BT /F1 12 Tf 72 640 Td (" + text.encode("ascii") + b") Tj ET"
        stream = DecodedStreamObject()
        stream.set_data(content)
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = BytesIO()
        writer.write(output)
        data = output.getvalue()
        source_text = PdfReader(BytesIO(data)).pages[8].extract_text()
        uploaded = self.client.post("/documents", files={"file": ("fixture.pdf", data, "application/pdf")}).json()
        parsed = self.client.post(f"/documents/{uploaded['document_id']}/parse", json={"chunk_size": 500})
        self.assertEqual(parsed.status_code, 200)
        return uploaded["document_id"], source_text

    def write_records(self, records):
        self.registry_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    def context(self):
        return build_reviewed_context(self.raw, self.settings.upload_directory)

    def draft(self):
        statement = self.record["reviewed_statement"]
        return {"decision": "answer", "claims": [{"text": statement, "citations": [{"citation_id": "E1", "quote": statement}]}], "reason": None}

    def install_model(self, draft=None, handler=None, max_calls=3):
        draft = self.draft() if draft is None else draft
        def respond(request):
            self.requests.append(request)
            if handler:
                return handler(request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(draft)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
        self.app.state.deepseek = DeepSeekClient(DeepSeekSettings(enabled=True, api_key="offline-review-fixture", max_calls=max_calls),
                                               transport=httpx.MockTransport(respond))


class ReviewedFormulaTests(ReviewFixture, unittest.TestCase):
    def test_context_preserves_raw_text_and_distinguishes_reviewed_annotation(self):
        before = copy.deepcopy(self.raw)
        packet = self.context()
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(packet["included_count"], 1)
        self.assertFalse(packet["raw_text_modified"])
        self.assertEqual(packet["source_kind"], "reviewed_formula_annotation")
        self.assertEqual(packet["raw_context_id"], self.raw["context_id"])
        self.assertNotEqual(packet["reviewed_context_id"], packet["raw_context_id"])
        self.assertEqual(packet["evidence"][0]["raw_quote"], self.source_text)
        self.assertEqual(packet["evidence"][0]["review_method"], "assistant_visual_review")
        self.assertEqual(self.raw, before)
        model_input = packet["context_text"]
        self.assertNotIn(self.source_text, model_input)
        self.assertNotIn(self.document_id, model_input)

    def test_registry_revision_changes_context_without_modifying_raw_context(self):
        before = self.context()
        self.record["reviewed_statement"] += " Reviewed."
        self.write_records([self.record])
        after = self.context()
        self.assertNotEqual(before["reviewed_context_id"], after["reviewed_context_id"])
        self.assertNotEqual(before["evidence"][0]["review_revision"], after["evidence"][0]["review_revision"])
        self.assertEqual(before["raw_context_id"], after["raw_context_id"])

    def test_wrong_file_hash_never_applies_lecture_correction(self):
        self.record["source_sha256"] = "f" * 64
        self.write_records([self.record])
        self.assertEqual(self.context()["status"], "no_reviewed_evidence")

    def test_mismatched_quote_and_range_fail_closed(self):
        for changes in ({"raw_quote": "x" * len(self.source_text)}, {"raw_start_char": 1}, {"raw_end_char": 999}):
            self.write_records([{**self.record, **changes}])
            with self.assertRaises(AnswerCheckError):
                self.context()

    def test_registry_with_wrong_page_is_not_fallback_evidence(self):
        self.record["pdf_page_number"] = 8
        self.write_records([self.record])
        self.assertEqual(self.context()["evidence"], [])

    def test_actual_pdf_is_reextracted_not_only_trusting_saved_json(self):
        with patch("src.reviewed_formula.PdfReader", side_effect=ValueError("private parse failure")):
            with self.assertRaises(AnswerCheckError) as caught:
                self.context()
        self.assertNotIn("private", str(caught.exception))

    def test_registry_schema_duplicate_ids_and_locations_are_rejected(self):
        cases = [[self.record, self.record], [{**self.record, "review_method": "human_verified"}],
                 [{**self.record, "scope": "entire_page"}], [{**self.record, "unexpected": True}],
                 [{**self.record, "reviewed_statement": " "}], [{**self.record, "reviewed_at": "2026-99-99"}],
                 [{**self.record, "formula_latex": " "}], {"not": "a list"}]
        for records in cases:
            with self.subTest(records_type=type(records).__name__):
                self.write_records(records)
                with self.assertRaises(AnswerCheckError):
                    load_formula_reviews(self.registry_path)

    def test_missing_oversized_or_duplicate_json_registry_is_rejected(self):
        with self.assertRaises(AnswerCheckError):
            load_formula_reviews(self.root / "missing.json")
        for content in ("x" * (MAX_REGISTRY_BYTES + 1), '[{"a":1,"a":2}]', '[NaN]'):
            self.registry_path.write_text(content, encoding="utf-8")
            with self.assertRaises(AnswerCheckError):
                load_formula_reviews(self.registry_path)

    def test_no_retrieved_evidence_skips_registry_and_pdf_read(self):
        packet = self.client.post(self.url + "/context", json={**self.query, "query": "nomatchinguniquephrase"}).json()
        with patch("src.reviewed_formula.load_formula_reviews", side_effect=AssertionError("must skip")):
            reviewed = build_reviewed_context(packet, self.settings.upload_directory)
        self.assertEqual(reviewed["status"], "no_evidence")

    def test_reviewed_text_has_a_separate_character_budget(self):
        raw = self.client.post(self.url + "/context", json={**self.query, "max_context_chars": 300}).json()
        self.assertTrue(raw["evidence"])
        self.record["reviewed_statement"] = "x" * 500
        self.write_records([self.record])
        packet = build_reviewed_context(raw, self.settings.upload_directory)
        self.assertEqual(packet["status"], "budget_exceeded")
        self.assertEqual(packet["used_chars"], 0)

    def test_tampered_context_and_note_revision_are_rejected(self):
        for change in (lambda packet: packet.update(query="different"),
                       lambda packet: packet["evidence"][0].update(review_revision="0" * 64),
                       lambda packet: packet.update(included_count=2)):
            packet = self.context()
            change(packet)
            with self.assertRaises(AnswerCheckError):
                validate_reviewed_context(packet)

    def test_checked_citation_contains_raw_and_reviewed_text_without_false_pdf_offsets(self):
        checked = check_reviewed_draft(self.context(), self.draft())
        self.assertEqual(checked["status"], "passed")
        self.assertFalse(checked["semantics_verified"])
        citation = checked["claims"][0]["citations"][0]
        self.assertEqual(citation["quote"], self.record["reviewed_statement"])
        self.assertEqual(citation["raw_quote"], self.source_text)
        self.assertEqual(citation["raw_start_char"], 0)
        self.assertEqual(citation["raw_end_char"], len(self.source_text))
        self.assertEqual(citation["document_id"], self.document_id)
        self.assertEqual(citation["pdf_page_number"], 9)
        self.assertNotIn("quote_start_char", citation)

    def test_known_wrong_condition_and_novel_proof_cannot_pass_with_a_real_quote(self):
        for text in ("Equality holds iff zw >= 0.", "A new unsupported proof."):
            draft = self.draft()
            draft["claims"][0]["text"] = text
            checked = check_reviewed_draft(self.context(), draft)
            self.assertEqual(checked["status"], "rejected")
            self.assertEqual(checked["claims"], [])
            self.assertEqual(checked["issues"][0]["code"], "reviewed_statement_mismatch")

    def test_unknown_reference_raw_quote_and_duplicate_selection_are_rejected(self):
        cases = []
        unknown = self.draft()
        unknown["claims"][0]["citations"][0]["citation_id"] = "E99"
        cases.append(unknown)
        raw = self.draft()
        raw["claims"][0]["citations"][0]["quote"] = self.source_text
        cases.append(raw)
        duplicate = self.draft()
        duplicate["claims"] *= 2
        cases.append(duplicate)
        multiple = self.draft()
        multiple["claims"][0]["citations"] *= 2
        cases.append(multiple)
        for draft in cases:
            self.assertEqual(check_reviewed_draft(self.context(), draft)["status"], "rejected")

    def test_invalid_or_empty_drafts_are_rejected(self):
        for draft in ([], {"decision": "unknown"}, {"decision": "answer", "claims": []},
                      {"decision": "answer", "claims": [], "reason": "contradiction"}):
            self.assertEqual(check_reviewed_draft(self.context(), draft)["status"], "rejected")

    def test_refusal_requires_reason_without_claims_or_inline_ids(self):
        self.assertEqual(check_reviewed_draft(self.context(), {"decision": "refuse", "reason": "Only a conclusion is available, not a proof."})["status"], "passed")
        for draft in ({"decision": "refuse"}, {"decision": "refuse", "reason": " "},
                      {"decision": "refuse", "reason": "[E1]"},
                      {"decision": "refuse", "reason": "reason", "claims": self.draft()["claims"]}):
            self.assertEqual(check_reviewed_draft(self.context(), draft)["status"], "rejected")

    def test_no_reviewed_evidence_refuses_locally_without_model_call(self):
        self.write_records([])
        self.install_model()
        answer = generate_reviewed_answer(self.context(), self.app.state.deepseek)
        self.assertEqual(answer["status"], "refused")
        self.assertFalse(answer["generation"]["model_called"])
        self.assertEqual(self.requests, [])


class ReviewedFormulaApiTests(ReviewFixture, unittest.TestCase):
    def test_stale_preflight_id_blocks_before_model_attempt(self):
        packet = self.client.post(self.url + "/reviewed-context", json=self.query).json()
        self.install_model()
        self.record["reviewed_statement"] += " Updated."
        self.write_records([self.record])
        response = self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True,
            "expected_reviewed_context_id": packet["reviewed_context_id"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.app.state.deepseek.call_state()["calls_used"], 0)

    def test_matching_preflight_id_and_invalid_id_are_explicit(self):
        packet = self.client.post(self.url + "/reviewed-context", json=self.query).json()
        self.install_model()
        response = self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True,
            "expected_reviewed_context_id": "bad"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.requests, [])
        response = self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True,
            "expected_reviewed_context_id": packet["reviewed_context_id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "answered")

    def test_free_preview_does_not_call_model_and_old_raw_answer_still_blocks(self):
        with patch.object(self.app.state.deepseek, "complete", side_effect=AssertionError("no paid model")):
            preview = self.client.post(self.url + "/reviewed-context", json=self.query)
            raw_answer = self.client.post(self.url + "/answer", json={**self.query, "allow_external": True})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["status"], "ready")
        self.assertEqual(raw_answer.json()["status"], "refused")
        self.assertTrue(raw_answer.json()["quality"]["blocks_generation"])

    def test_model_only_sees_reviewed_statements_and_shares_existing_usage_reporting(self):
        self.install_model()
        response = self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "answered")
        self.assertEqual(body["source_kind"], "reviewed_formula_annotation")
        self.assertEqual(body["generation"]["calls_used"], 1)
        self.assertEqual(body["generation"]["usage"]["total_tokens"], 120)
        prompt = self.requests[0].content.decode()
        self.assertNotIn(self.source_text, prompt)
        self.assertNotIn(self.document_id, prompt)
        self.assertNotIn(self.raw["source_sha256"], prompt)
        self.assertNotIn(str(self.root), prompt)
        sent = json.loads(json.loads(prompt)["messages"][1]["content"])
        self.assertEqual(set(sent), {"question", "evidence"})
        self.assertEqual(sent["evidence"][0]["reviewed_statement"], self.record["reviewed_statement"])

    def test_wrong_model_claim_is_rejected_without_partial_delivery(self):
        draft = self.draft()
        draft["claims"][0]["text"] = "Equality holds iff zw >= 0."
        self.install_model(draft)
        response = self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "rejected")
        self.assertEqual(response.json()["check"]["claims"], [])

    def test_model_can_refuse_when_reviewed_statement_is_not_a_full_proof(self):
        self.install_model({"decision": "refuse", "reason": "The available statement does not contain a complete proof."})
        response = self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "refused")
        self.assertFalse(response.json()["check"]["semantics_verified"])

    def test_shared_model_quota_also_applies_to_reviewed_mode(self):
        self.install_model(max_calls=1)
        self.assertEqual(self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True}).status_code, 200)
        other, _ = self.prepare(text="alpha ordinary text", bar=False)
        response = self.client.post(f"/documents/{other}/answer", json={**self.query, "allow_external": True})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(self.requests), 1)

    def test_no_matching_note_never_calls_model_or_falls_back_to_raw_text(self):
        self.install_model()
        other, _ = self.prepare(text="alpha different content")
        response = self.client.post(f"/documents/{other}/answer-reviewed", json={**self.query, "allow_external": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "refused")
        self.assertFalse(response.json()["generation"]["model_called"])
        self.assertEqual(self.requests, [])

    def test_consent_and_server_owned_evidence_are_required(self):
        self.install_model()
        for changes in ({"allow_external": False}, {"allow_external": 1}, {"reviews": []},
                        {"registry_path": str(self.registry_path)}, {"api_key": "not-accepted"}):
            response = self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True, **changes})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.post(self.url + "/answer-reviewed", json=self.query).status_code, 422)
        self.assertEqual(self.requests, [])

    def test_source_change_during_model_call_discards_reviewed_answer(self):
        def handler(request):
            (self.settings.upload_directory / f"{self.document_id}.pdf").write_bytes(b"%PDF-1.7\nchanged")
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(self.draft())}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
        self.install_model(handler=handler)
        response = self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("claims", response.json())

    def test_annotation_change_during_call_discards_answer_even_when_pdf_did_not_change(self):
        def handler(request):
            self.record["reviewed_statement"] += " A changed review."
            self.write_records([self.record])
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(self.draft())}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
        self.install_model(handler=handler)
        response = self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True})
        self.assertEqual(response.status_code, 409)
        self.assertIn("核对注释已改变", response.json()["detail"])

    def test_missing_document_and_bad_registry_are_explicit_and_free(self):
        self.install_model()
        self.assertEqual(self.client.post("/documents/" + "0" * 32 + "/reviewed-context", json=self.query).status_code, 404)
        self.registry_path.write_text("bad registry", encoding="utf-8")
        self.assertEqual(self.client.post(self.url + "/reviewed-context", json=self.query).status_code, 409)
        self.assertEqual(self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True}).status_code, 409)
        self.assertEqual(self.requests, [])

    def test_disabled_model_and_sanitized_upstream_failure(self):
        self.assertEqual(self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True}).status_code, 503)
        self.install_model(handler=lambda request: httpx.Response(500, text="private upstream body"))
        response = self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private upstream", response.text)
        self.assertEqual(len(self.requests), 1)

    def test_document_files_and_raw_context_remain_unchanged(self):
        def files():
            return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.settings.data_dir.rglob("*") if path.is_file()}
        self.install_model()
        before = files()
        self.client.post(self.url + "/reviewed-context", json=self.query)
        self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True})
        self.assertEqual(before, files())
        self.assertEqual(self.raw, self.client.post(self.url + "/context", json=self.query).json())

    def test_openapi_discloses_distinct_source_and_paid_selection_mode(self):
        schema = self.client.get("/openapi.json").json()
        self.assertIn("不调用模型", schema["paths"]["/documents/{document_id}/reviewed-context"]["post"]["summary"])
        self.assertIn("不新增证明", schema["paths"]["/documents/{document_id}/answer-reviewed"]["post"]["summary"])
        properties = schema["components"]["schemas"]["ReviewedFormulaCitation"]["properties"]
        self.assertEqual(properties["source_kind"]["const"], "reviewed_formula_annotation")
        self.assertIn("raw_start_char", properties)
        self.assertNotIn("quote_start_char", properties)


if __name__ == "__main__":
    unittest.main()
