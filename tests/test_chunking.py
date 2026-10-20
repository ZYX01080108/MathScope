import unittest

from src.chunking import chunk_page


class ChunkingTests(unittest.TestCase):
    def test_empty_page_produces_no_chunks(self):
        page = {
            "document_name": "example.pdf",
            "pdf_page_number": 2,
            "text": "",
        }

        self.assertEqual(chunk_page(page, chunk_size=4), [])

    def test_invalid_chunk_sizes_are_rejected(self):
        page = {
            "document_name": "example.pdf",
            "pdf_page_number": 1,
            "text": "ABCDEFGHIJ",
        }

        for size in (0, -1, 2.5, "4", True):
            with self.subTest(chunk_size=size):
                with self.assertRaises(ValueError):
                    chunk_page(page, chunk_size=size)
    def test_chunks_preserve_text_offsets_and_source(self):
        page = {
            "document_name": "example.pdf",
            "pdf_page_number": 7,
            "text": "ABCDEFGHIJ",
        }

        chunks = chunk_page(page, chunk_size=4)

        self.assertEqual(
            [
                (chunk["start_char"], chunk["end_char"], chunk["text"])
                for chunk in chunks
            ],
            [
                (0, 4, "ABCD"),
                (4, 8, "EFGH"),
                (8, 10, "IJ"),
            ],
        )

        for chunk in chunks:
            self.assertEqual(chunk["document_name"], "example.pdf")
            self.assertEqual(chunk["pdf_page_number"], 7)

        self.assertEqual(
            "".join(chunk["text"] for chunk in chunks),
            page["text"],
        )