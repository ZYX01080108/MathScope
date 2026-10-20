import unittest

from src.search import search_chunks


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.chunks = [
            {
                "document_name": "example.pdf",
                "pdf_page_number": 3,
                "start_char": 0,
                "end_char": 10,
                "text": "Stack: LIFO",
            },
            {
                "document_name": "example.pdf",
                "pdf_page_number": 8,
                "start_char": 0,
                "end_char": 11,
                "text": "Queue: FIFO",
            },
        ]

    def test_matching_ignores_case_and_query_outer_whitespace(self):
        matches = search_chunks(self.chunks, "  STACK  ")

        self.assertEqual(matches, [self.chunks[0]])

    def test_no_match_returns_empty_list(self):
        self.assertEqual(search_chunks(self.chunks, "binary tree"), [])

    def test_blank_query_is_rejected(self):
        for query in ("", "   ", "\n\t"):
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    search_chunks(self.chunks, query)