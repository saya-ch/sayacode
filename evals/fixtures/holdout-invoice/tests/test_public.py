import unittest
from decimal import Decimal

from billing import subtotal, with_tax
from models import Invoice, Line


class PublicTests(unittest.TestCase):
    def test_amount(self):
        invoice = Invoice([Line("book", Decimal("4.00"), 2)])
        self.assertEqual(subtotal(invoice), Decimal("8.00"))

    def test_zero_tax(self):
        self.assertEqual(with_tax(Decimal("2.00"), Decimal("0")), Decimal("2.00"))
