import tempfile
import unittest
from pathlib import Path

from notes import save_note


class PublicTests(unittest.TestCase):
    def test_save(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "note.txt"
            save_note(target, " title ", "hello")
            self.assertEqual(target.read_text(encoding="utf-8"), "title\nhello")
