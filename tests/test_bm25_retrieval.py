import unittest

from src.bm25_retrieval import BM25Retriever


class BM25RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.chunks = [
            {
                "document_name": "test.pdf",
                "pdf_page_number": index + 1,
                "start_char": 0,
                "end_char": len(text),
                "text": text,
            }
            for index, text in enumerate(
                ["alpha beta", "alpha alpha", "gamma"]
            )
        ]
        self.retriever = BM25Retriever(
            self.chunks, tokenizer=str.split
        )

    def test_top_k_preserves_source(self):
        hits = self.retriever.search("alpha", k=1)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["chunk"], self.chunks[1])
        self.assertNotIn("score", self.chunks[1])

    def test_no_overlap_returns_empty(self):
        self.assertEqual(self.retriever.search("delta"), [])

    def test_invalid_input_is_rejected(self):
        with self.assertRaises(ValueError):
            self.retriever.search("   ")

        for k in (0, -1, True, 1.5):
            with self.subTest(k=k):
                with self.assertRaises(ValueError):
                    self.retriever.search("alpha", k=k)

    def test_zero_score_match_is_retained(self):
        retriever = BM25Retriever(
            self.chunks[:2], tokenizer=str.split
        )
        hits = retriever.search("beta")

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["chunk"], self.chunks[0])
        self.assertEqual(hits[0]["score"], 0.0)