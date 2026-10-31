import unittest

from stats import mean, percentile


class PublicTests(unittest.TestCase):
    def test_mean(self):
        self.assertEqual(mean([1, 3]), 2)

    def test_percentile_endpoint(self):
        self.assertEqual(percentile([9, 2, 4], 100), 9)
