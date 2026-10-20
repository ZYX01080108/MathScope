import unittest
from src.validation import validate_page
class PageValidationTests(unittest.TestCase):
    def test_first_page_is_valid(self):
        page={
            "document_name": "sample.pdf",
            "pdf_page_number": 1,
            "text": "示例文本",
        }
        validate_page(page)
    def test_zero_page_is_rejected(self):
        page = {
            "document_name": "sample.pdf",
            "pdf_page_number": 0,
            "text": "示例文本",
        }

        with self.assertRaises(ValueError):
            validate_page(page)