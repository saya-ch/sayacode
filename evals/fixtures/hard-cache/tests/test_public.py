import unittest

from cache import TTLCache


class PublicTests(unittest.TestCase):
    def test_put_get(self):
        cache = TTLCache(capacity=2, ttl=10, clock=lambda: 0.0)
        cache.put("a", 1.0)
        self.assertEqual(cache.get("a"), 1.0)

    def test_missing(self):
        cache = TTLCache(capacity=2, ttl=10, clock=lambda: 0.0)
        self.assertIsNone(cache.get("nope"))
