import unittest

from ledger import Ledger


class PublicTests(unittest.TestCase):
    def test_balanced_post_balances(self):
        ledger = Ledger()
        ledger.post(
            "e1",
            [("cash", "debit", "100.00"), ("revenue", "credit", "100.00")],
        )
        balance = ledger.trial_balance()
        self.assertEqual(balance["cash"] + balance["revenue"], 0)
        self.assertEqual(balance["cash"], 100)

    def test_second_post_accumulates(self):
        ledger = Ledger()
        ledger.post("e1", [("cash", "debit", "10.00"), ("revenue", "credit", "10.00")])
        ledger.post("e2", [("cash", "debit", "5.00"), ("revenue", "credit", "5.00")])
        self.assertEqual(ledger.trial_balance()["cash"], 15)
