import unittest

from models import Note
from render import render


class PublicTests(unittest.TestCase):
    def test_markdown(self):
        self.assertEqual(render([Note(1, "T", "B")]), "# T\nB")
