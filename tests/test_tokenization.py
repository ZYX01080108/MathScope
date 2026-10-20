import unittest

from src.tokenization import tokenize
from src.tokenization import tokenize, tokenize_normalized
class TokenizationTests(unittest.TestCase):
    def test_shared_terms_survive_and_punctuation_is_removed(self):
        query = tokenize("为什么复数域不能成为有序域？")
        passage = tokenize("复数域 C 不是一个有序数域。")

        self.assertIn("复数", query)
        self.assertIn("复数", passage)
        self.assertIn("域", query)
        self.assertIn("域", passage)
        self.assertIn("c", passage)

        self.assertNotIn("？", query)
        self.assertNotIn("。", passage)
    def test_ordered_field_terms_are_normalized_consistently(self):
        self.assertEqual(
            tokenize_normalized("有序数域"),
            tokenize_normalized("有序域"),
        )