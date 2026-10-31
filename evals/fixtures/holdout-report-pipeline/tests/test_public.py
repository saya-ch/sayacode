import unittest
from decimal import Decimal

from report import summarize


class PublicTests(unittest.TestCase):
    def test_sum(self):
        self.assertEqual(summarize([{"region": "east", "amount": "2.5"}]), {"east": Decimal("2.5")})
