import tempfile
import unittest
from pathlib import Path

from importer import load_items


class PublicTests(unittest.TestCase):
    def test_basic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "items.csv"
            path.write_text("sku,count\na,2\n", encoding="utf-8")
            self.assertEqual(load_items(path), [{"sku": "a", "count": 2}])
