import copy
import hashlib
import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from src.answer_check import AnswerCheckError, check_answer_draft
from src.answer_generation import generate_answer
from src.deepseek_client import DeepSeekClient, DeepSeekSettings
from src.document_upload import save_pdf_upload
from src.evidence_context import build_evidence_context
from src.formula_quality import (
    MAX_CONTENT_BYTES, MAX_PAGE_OPERATIONS, assess_document_formula_quality,
    assess_page_operations, build_quality_report, validate_quality_report,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "triangle-conjugate-regression.json"


def pdf_bytes(content=b"", page_count=1):
    writer = PdfWriter()
    for number in range(page_count):
        page = writer.add_blank_page(width=612, height=792)
        if number == page_count - 1 and content:
            stream = DecodedStreamObject()
            stream.set_data(content)
            page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def packet_for(text="alpha beta", document_id="a" * 32, sha="b" * 64, page=1):
    results = [{"score": 1.0, "chunk": {"document_id": document_id, "document_name": "fixture.pdf",
        "pdf_page_number": page, "start_char": 0, "end_char": len(text), "text": text}}] if text else []
    return build_evidence_context({"document_id": document_id, "document_name": "fixture.pdf", "source_sha256": sha,
        "query": "alpha", "method": "normalized", "k": 10, "indexed_chunk_count": len(results),
        "returned_count": len(results), "cache_hit": False, "results": results})


class FormulaGeometryTests(unittest.TestCase):
    def test_observed_lecture_overbar_is_flagged_but_not_restored(self):
        case = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        bar = case["observed_bar"]
        report = assess_page_operations([([], b"q"), ([bar["line_width"]], b"w"),
            (bar["start"], b"m"), (bar["end"], b"l"), ([], b"S"), ([], b"Q")], 9)
        self.assertEqual(report["status"], "risk_detected")
        self.assertEqual(report["short_horizontal_mark_count"], 1)

    def test_filled_thin_horizontal_bar_is_flagged(self):
        report = assess_page_operations([([10, 20, 8, 0.4], b"re"), ([], b"f")], 1)
        self.assertEqual(report["status"], "risk_detected")

    def test_vertical_long_thick_or_unpainted_line_is_not_flagged(self):
        for width, end, paint in ((0.4, [0, 8], b"S"), (0.4, [200, 0], b"S"),
                                  (4, [8, 0], b"S"), (0.4, [8, 0], b"n")):
            report = assess_page_operations([([width], b"w"), ([0, 0], b"m"), (end, b"l"), ([], paint)], 1)
            self.assertEqual(report["status"], "not_flagged")
        self.assertEqual(assess_page_operations([], 1)["status"], "not_flagged")

    def test_stroke_width_is_restored_across_graphics_save_and_restore(self):
        ops = [([4], b"w"), ([], b"q"), ([0.4], b"w"), ([0, 0], b"m"), ([8, 0], b"l"), ([], b"S"),
               ([], b"Q"), ([0, 0], b"m"), ([8, 0], b"l"), ([], b"S")]
        self.assertEqual(assess_page_operations(ops, 1)["short_horizontal_mark_count"], 1)

    def test_nontranslation_matrix_xobject_clipping_or_curve_is_unassessed(self):
        for op in b"Do", b"sh", b"INLINE IMAGE", b"W", b"c":
            self.assertEqual(assess_page_operations([([], op)], 1)["status"], "unassessed")
        self.assertEqual(assess_page_operations([([2, 0, 0, 2, 0, 0], b"cm")], 1)["status"], "unassessed")
        self.assertEqual(assess_page_operations([([1, 0, 0, 1, 72, 720], b"cm")], 1)["status"], "not_flagged")

    def test_invalid_operands_unbalanced_states_and_operation_limit_are_unassessed(self):
        for ops in ([([float("nan")], b"w")], [([], b"Q")], [([], b"q")], [([0], b"m")],
                    [([-1], b"w")], [([], b"n")] * (MAX_PAGE_OPERATIONS + 1)):
            self.assertEqual(assess_page_operations(ops, 1)["status"], "unassessed")

    def test_risk_is_not_erased_by_an_unsupported_operator(self):
        ops = [([0.4], b"w"), ([0, 0], b"m"), ([8, 0], b"l"), ([], b"S"), ([], b"Do")]
        report = assess_page_operations(ops, 1)
        self.assertEqual(report["status"], "risk_detected")
        self.assertIn("unsupported_graphics", report["reasons"])


class FormulaReportTests(unittest.TestCase):
    def test_missing_assessment_blocks_generation_and_does_not_spend(self):
        response = generate_answer(packet_for(), DeepSeekClient(DeepSeekSettings()))
        self.assertEqual(response["quality"]["status"], "unassessed")
        self.assertEqual(response["status"], "refused")
        self.assertFalse(response["generation"]["model_called"])
        self.assertEqual(response["generation"]["calls_used"], 0)

    def test_no_evidence_needs_no_pdf_scan(self):
        packet = packet_for(text=None)
        with patch("src.formula_quality.get_document_file", side_effect=AssertionError("must skip read")):
            report = assess_document_formula_quality(packet, Path("unused"))
        self.assertEqual(report["status"], "no_evidence")
        self.assertEqual(report["checked_page_numbers"], [])

    def test_quality_source_context_pages_and_aggregate_cannot_be_swapped(self):
        packet = packet_for()
        report = build_quality_report(packet, [assess_page_operations([], 1)])
        for field, value in (("source_sha256", "c" * 64), ("context_id", "0" * 64),
                             ("document_id", "d" * 32), ("checked_page_numbers", [2]),
                             ("blocks_generation", True), ("status", "unassessed"), ("text_modified", True)):
            changed = {**report, field: value}
            with self.subTest(field=field):
                with self.assertRaises(AnswerCheckError):
                    validate_quality_report(packet, changed)

    def test_page_count_and_page_status_inconsistency_are_rejected(self):
        packet = packet_for()
        with self.assertRaises(AnswerCheckError):
            build_quality_report(packet, [assess_page_operations([], 2)])
        with self.assertRaises(AnswerCheckError):
            build_quality_report(packet, [])
        report = build_quality_report(packet, [assess_page_operations([], 1)])
        for changes in ({"short_horizontal_mark_count": 1}, {"reasons": ["page_unreadable"]}):
            changed = copy.deepcopy(report)
            changed["pages"][0].update(changes)
            with self.assertRaises(AnswerCheckError):
                validate_quality_report(packet, changed)

    def test_known_incorrect_claim_passes_literal_check_but_guard_blocks_before_model(self):
        case = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        packet = packet_for(case["extracted_quote"], sha=case["source_sha256"], page=9)
        draft = {"decision": "answer", "claims": [{"text": case["incorrect_claim"],
            "citations": [{"citation_id": "E1", "quote": case["extracted_quote"]}]}]}
        self.assertEqual(check_answer_draft(packet, draft)["status"], "passed")
        ops = [([0.478], b"w"), ([166.99, -70.35], b"m"), ([175.68, -70.35], b"l"), ([], b"S")]
        quality = build_quality_report(packet, [assess_page_operations(ops, 9)])
        client = DeepSeekClient(DeepSeekSettings())
        with patch.object(client, "complete", side_effect=AssertionError("must not call model")):
            response = generate_answer(packet, client, formula_quality=quality)
        self.assertEqual(response["status"], "refused")
        self.assertEqual(response["check"]["claims"], [])
        self.assertFalse(response["generation"]["model_called"])
        self.assertFalse(response["quality"]["text_modified"])
        self.assertEqual(packet["evidence"][0]["text"], case["extracted_quote"])

    def test_mathematical_counterexample_demonstrates_why_missing_conjugate_matters(self):
        case = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        z, w = (complex(*case["counterexample"][key]) for key in ("z", "w"))
        self.assertEqual(abs(z + w), abs(z) + abs(w))
        self.assertLess((z * w).real, 0)
        self.assertEqual(z * w.conjugate(), 1)


class FormulaPdfTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def uploaded_packet(self, content=b"", page_count=1):
        uploaded = save_pdf_upload(BytesIO(pdf_bytes(content, page_count)), "fixture.pdf", self.root)
        return packet_for(document_id=uploaded["document_id"], sha=uploaded["sha256"], page=page_count)

    def test_real_content_stream_scan_flags_bar_without_changing_files(self):
        packet = self.uploaded_packet(b"q 0.478 w 166.99 200 m 175.68 200 l S Q", 9)
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.iterdir()}
        quality = assess_document_formula_quality(packet, self.root)
        self.assertEqual(quality["status"], "risk_detected")
        self.assertEqual(quality["checked_page_numbers"], [9])
        self.assertEqual(quality["pages"][0]["short_horizontal_mark_count"], 1)
        self.assertEqual(before, {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.iterdir()})

    def test_blank_page_is_not_flagged_but_not_certified(self):
        quality = assess_document_formula_quality(self.uploaded_packet(), self.root)
        self.assertEqual(quality["status"], "not_flagged")
        self.assertFalse(quality["blocks_generation"])
        self.assertIn("不代表", quality["pages"][0]["detail"])

    def test_changed_source_fails_version_check(self):
        packet = self.uploaded_packet()
        (self.root / (packet["document_id"] + ".pdf")).write_bytes(b"%PDF-1.7\nchanged")
        with self.assertRaises(ValueError) as caught:
            assess_document_formula_quality(packet, self.root)
        self.assertEqual(caught.exception.status_code, 409)

    def test_unreadable_pdf_invalid_page_and_limits_are_not_flagged_as_safe(self):
        packet = self.uploaded_packet()
        readers = [ValueError("private error"), SimpleNamespace(is_encrypted=True, pages=[]),
                   SimpleNamespace(is_encrypted=False, pages=[])]
        for reader in readers:
            with patch("src.formula_quality.PdfReader", side_effect=reader if isinstance(reader, Exception) else None,
                       return_value=reader if not isinstance(reader, Exception) else None):
                report = assess_document_formula_quality(packet, self.root)
            self.assertEqual(report["status"], "unassessed")
            self.assertNotIn("private", json.dumps(report))
        content = SimpleNamespace(get_data=lambda: b"x" * (MAX_CONTENT_BYTES + 1))
        reader = SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(get_contents=lambda: content)])
        with patch("src.formula_quality.PdfReader", return_value=reader):
            report = assess_document_formula_quality(packet, self.root)
        self.assertEqual(report["status"], "unassessed")
        self.assertIn("scan_limit", report["pages"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
