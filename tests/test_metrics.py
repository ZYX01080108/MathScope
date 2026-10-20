import unittest

from src.metrics import (
    recall_at_k,
    reciprocal_rank_at_k,
    ndcg_at_k,
)


class RetrievalMetricsTests(unittest.TestCase):
    def test_partial_retrieval_and_first_relevant_rank(self):
        ranked = ["X", "A", "B"]
        relevant = {"A", "B"}

        self.assertEqual(recall_at_k(ranked, relevant, 2), 0.5)
        self.assertEqual(recall_at_k(ranked, relevant, 3), 1.0)
        self.assertEqual(
            reciprocal_rank_at_k(ranked, relevant, 3), 0.5
        )

    def test_no_hit_within_k(self):
        ranked = ["X", "A"]

        self.assertEqual(recall_at_k(ranked, {"A"}, 1), 0.0)
        self.assertEqual(
            reciprocal_rank_at_k(ranked, {"A"}, 1), 0.0
        )

    def test_no_relevant_labels_is_not_scored(self):
        self.assertIsNone(recall_at_k(["X"], set(), 5))
        self.assertIsNone(
            reciprocal_rank_at_k(["X"], set(), 5)
        )

    def test_invalid_k_is_rejected(self):
        for metric in (recall_at_k, reciprocal_rank_at_k, ndcg_at_k):
            for k in (0, -1, True, 1.5):
                with self.subTest(metric=metric.__name__, k=k):
                    with self.assertRaises(ValueError):
                        metric(["A"], {"A"}, k)

    def test_ndcg_is_one_for_ideal_ranking(self):
        self.assertAlmostEqual(
            ndcg_at_k(["A", "B", "X"], {"A", "B"}, 3),
            1.0,
        )

    def test_ndcg_discounts_later_relevant_results(self):
        self.assertAlmostEqual(
            ndcg_at_k(["X", "A", "B"], {"A", "B"}, 3),
            0.6934264036,
        )

    def test_ndcg_handles_no_hit_and_no_labels(self):
        self.assertEqual(
            ndcg_at_k(["X"], {"A"}, 5), 0.0
        )
        self.assertIsNone(
            ndcg_at_k(["X"], set(), 5)
        )

    def test_ndcg_does_not_reward_duplicate_results(self):
        self.assertAlmostEqual(
            ndcg_at_k(["A", "A"], {"A", "B"}, 2),
            0.6131471928,
        )
