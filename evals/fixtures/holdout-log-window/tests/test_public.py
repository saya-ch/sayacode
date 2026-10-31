import unittest
from datetime import datetime, timezone

from logbook import Entry, LogBook


class PublicTests(unittest.TestCase):
    def test_empty(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(LogBook().between(now, now), [])

    def test_single_recent(self):
        entry = Entry(datetime(2024, 1, 1, tzinfo=timezone.utc), "a")
        self.assertEqual(LogBook([entry]).recent(1), [entry])
