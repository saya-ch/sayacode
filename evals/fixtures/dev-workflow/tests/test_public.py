import unittest

from app import render


class PublicTests(unittest.TestCase):
    def test_csv(self):
        self.assertIn("name,score", render([{"name": "A", "score": 2}]))
