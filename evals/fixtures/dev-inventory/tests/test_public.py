import unittest

from inventory import Inventory, discount


class PublicTests(unittest.TestCase):
    def test_add(self):
        stock = Inventory({})
        stock.add("pen", 2)
        self.assertEqual(stock.count("pen"), 2)

    def test_discount(self):
        self.assertEqual(discount(100, 0.2), 80)
