import unittest
import json
from src.extraction import extract_pages

class FakePage:
    def __init__(self, text):
        self.text = text

    def extract_text(self):
        return self.text


class FakeReader:
    def __init__(self, texts):
        self.pages = [FakePage(text) for text in texts]


class ExtractPagesTests(unittest.TestCase):
    def test_empty_middle_page_preserves_page_mapping(self):
        reader = FakeReader(["第一页内容", "", "第三页内容"])

        pages = extract_pages(reader, "example.pdf")

        self.assertEqual(len(pages), 3)
        self.assertEqual(
            [page["pdf_page_number"] for page in pages],
            [1, 2, 3],
        )
        self.assertEqual(
            [page["text"] for page in pages],
            ["第一页内容", "", "第三页内容"],
        )
        self.assertEqual(
            [page["document_name"] for page in pages],
            ["example.pdf", "example.pdf", "example.pdf"],
        )