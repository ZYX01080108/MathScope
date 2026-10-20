import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.storage import write_jsonl


class PageStorageTests(unittest.TestCase):
    def test_chunk_round_trip_preserves_source_offsets(self):
        chunks = [
            {
                "document_name": "example.pdf",
                "pdf_page_number": 7,
                "start_char": 4,
                "end_char": 8,
                "text": "EFGH",
            },
        ]

        with TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "chunks.jsonl"

            write_jsonl(chunks, output_path)

            with output_path.open("r", encoding="utf-8") as file:
                loaded_chunks = [json.loads(line) for line in file]

            self.assertEqual(loaded_chunks, chunks)
    def test_jsonl_round_trip_preserves_pages(self):
        pages = [
            {
                "document_name": "数学讲义.pdf",
                "pdf_page_number": 1,
                "text": "第一行：定义\n第二行：条件",
            },
            {
                "document_name": "数学讲义.pdf",
                "pdf_page_number": 2,
                "text": "",
            },
        ]

        with TemporaryDirectory() as temporary_directory:
            output_path = (
                Path(temporary_directory) / "parsed" / "sample.pages.jsonl"
            )

            write_jsonl(pages, output_path)

            with output_path.open("r", encoding="utf-8") as file:
                lines = file.readlines()

            self.assertEqual(len(lines), 2)

            loaded_pages = [json.loads(line) for line in lines]
            self.assertEqual(loaded_pages, pages)