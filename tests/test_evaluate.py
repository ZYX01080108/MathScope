import unittest

from src.evaluate import compare_evidence


class EvidenceEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.a = {
            "document_name": "example.pdf",
            "pdf_page_number": 1,
            "start_char": 0,
            "end_char": 4,
        }
        self.b = {
            "document_name": "example.pdf",
            "pdf_page_number": 1,
            "start_char": 4,
            "end_char": 8,
        }

    def test_partial_match(self):
        matched, missing = compare_evidence(
            [self.a, self.b],
            [self.a],
        )

        self.assertEqual(matched, {("example.pdf", 1, 0, 4)})
        self.assertEqual(missing, {("example.pdf", 1, 4, 8)})

    def test_no_results_misses_all_targets(self):
        matched, missing = compare_evidence([self.a], [])

        self.assertEqual(matched, set())
        self.assertEqual(missing, {("example.pdf", 1, 0, 4)})

    def test_duplicate_results_count_only_once(self):
        matched, missing = compare_evidence(
            [self.a, self.b],
            [self.a, self.a],
        )

        self.assertEqual(len(matched), 1)
        self.assertEqual(missing, {("example.pdf", 1, 4, 8)})