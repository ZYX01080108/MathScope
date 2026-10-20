import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.evaluation_runner import (
    evidence_reference,
    file_sha256,
    run_evaluation,
    write_json_report,
)


class EvaluationRunnerTests(unittest.TestCase):
    """Small self-contained corpus; does not need the user's private PDF."""

    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "data/raw").mkdir(parents=True)
        (self.root / "data/parsed").mkdir(parents=True)
        (self.root / "docs").mkdir()
        (self.root / "data/raw/test.pdf").write_bytes(b"fixed-pdf-fixture")
        page = {"document_name": "test.pdf", "pdf_page_number": 1, "text": "alpha beta gamma"}
        self.chunks = [
            {"document_name": "test.pdf", "pdf_page_number": 1, "start_char": start, "end_char": end, "text": page["text"][start:end]}
            for start, end in ((0, 5), (6, 10), (11, 16))
        ]
        self._write_jsonl("data/parsed/test.page.jsonl", [page])
        self._write_jsonl("data/parsed/test.chunks.jsonl", self.chunks)
        self.manifest = {
            "schema_version": "1.0", "dataset_version": "test-v1", "split": "development",
            "annotation_status": "partial_target_evidence", "metric_scope": "test targets",
            "expected_page_count": 1, "expected_chunk_count": 3,
            "artifacts": {
                key: {"path": path, "sha256": file_sha256(self.root / path)}
                for key, path in (
                    ("pdf", "data/raw/test.pdf"),
                    ("pages", "data/parsed/test.page.jsonl"),
                    ("chunks", "data/parsed/test.chunks.jsonl"),
                )
            },
        }
        self.target = {**evidence_reference(self.chunks[0]), "evidence_quote": "alpha"}
        self.cases = [
            {"case_id": "Q001", "query": "alpha", "expected_behavior": "answer", "required_evidence": [self.target]},
            {"case_id": "Q002", "query": "delta", "expected_behavior": "insufficient_evidence", "required_evidence": []},
        ]
        self._save_inputs()

    def _write_json(self, path, value):
        (self.root / path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def _write_jsonl(self, path, records):
        text = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        (self.root / path).write_text(text, encoding="utf-8")

    def _save_inputs(self):
        self._write_json("docs/eval_manifest.json", self.manifest)
        self._write_json("docs/eval_cases.json", self.cases)

    def test_report_round_trip_and_negative_case_exclusion(self):
        report = run_evaluation(self.root)
        self.assertEqual(report["dataset"]["case_count"], 2)
        self.assertEqual(report["dataset"]["scored_case_count"], 1)
        self.assertEqual(report["input_fingerprints"]["artifacts"]["pdf"]["sha256"], self.manifest["artifacts"]["pdf"]["sha256"])
        for method in report["methods"]:
            self.assertFalse(method["cases"][1]["scored"])
            self.assertIsNone(method["cases"][1]["metrics"]["5"])
            for summary in method["summary"].values():
                self.assertEqual(summary["scored_case_count"], 1)
                self.assertEqual(summary["unscored_case_count"], 1)
                self.assertEqual(summary["mean_target_coverage"], 1.0)
                self.assertEqual(summary["target_mrr"], 1.0)
                self.assertEqual(summary["mean_target_ndcg"], 1.0)
        path = write_json_report(report, self.root / "reports/run.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)

    def test_stale_document_fingerprint_is_rejected(self):
        (self.root / "data/raw/test.pdf").write_bytes(b"a different version")
        with self.assertRaisesRegex(ValueError, "指纹不一致"):
            run_evaluation(self.root)

    def test_nonexistent_target_is_rejected(self):
        self.target["start_char"] = 1
        self._save_inputs()
        with self.assertRaisesRegex(ValueError, "不存在的块"):
            run_evaluation(self.root)

    def test_duplicate_case_ids_are_rejected(self):
        self.cases[1]["case_id"] = "Q001"
        self._save_inputs()
        with self.assertRaisesRegex(ValueError, "编号为空或重复"):
            run_evaluation(self.root)

    def test_answer_case_without_targets_is_rejected(self):
        self.cases[0]["required_evidence"] = []
        self._save_inputs()
        with self.assertRaisesRegex(ValueError, "没有目标证据"):
            run_evaluation(self.root)

    def test_negative_case_cannot_count_distractor_as_target(self):
        self.cases[1]["required_evidence"] = [self.target]
        self._save_inputs()
        with self.assertRaisesRegex(ValueError, "干扰证据"):
            run_evaluation(self.root)

    def test_chunk_must_equal_its_source_slice(self):
        self.chunks[0]["text"] = "wrong"
        self._write_jsonl("data/parsed/test.chunks.jsonl", self.chunks)
        self.manifest["artifacts"]["chunks"]["sha256"] = file_sha256(self.root / "data/parsed/test.chunks.jsonl")
        self._save_inputs()
        with self.assertRaisesRegex(ValueError, "块文本与原页不一致"):
            run_evaluation(self.root)

    def test_quote_must_belong_to_annotated_chunk(self):
        self.target["evidence_quote"] = "not in this chunk"
        self._save_inputs()
        with self.assertRaisesRegex(ValueError, "证据摘录"):
            run_evaluation(self.root)

    def test_evidence_identifiers_require_integer_positions(self):
        for field, value in (("pdf_page_number", True), ("start_char", 0.0)):
            with self.subTest(field=field, value=value):
                self.target.clear()
                self.target.update(evidence_reference(self.chunks[0]))
                self.target["evidence_quote"] = "alpha"
                self.target[field] = value
                self._save_inputs()
                with self.assertRaisesRegex(ValueError, "证据标识"):
                    run_evaluation(self.root)

    def test_report_never_overwrites_existing_file(self):
        output = self.root / "existing.json"
        output.write_text("keep this", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            write_json_report({"run_id": "new"}, output)
        self.assertEqual(output.read_text(encoding="utf-8"), "keep this")

    def test_k_values_are_validated_and_primary_k_is_preserved(self):
        for values in ((5, 5), (0, 5), (5, True), (10, 20), ()):
            with self.subTest(k_values=values):
                with self.assertRaises(ValueError):
                    run_evaluation(self.root, k_values=values)
