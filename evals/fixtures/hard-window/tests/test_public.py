import unittest

from window import p95, rolling_median, top_k_frequent


class PublicTests(unittest.TestCase):
    def test_p95_basic(self):
        self.assertEqual(p95([1, 2, 3, 4, 5]), 5)

    def test_rolling_median_single(self):
        self.assertEqual(rolling_median([5], 1), [5])

    def test_top_k_basic(self):
        self.assertEqual(top_k_frequent([1, 1, 2], 2), [(1, 2), (2, 1)])
