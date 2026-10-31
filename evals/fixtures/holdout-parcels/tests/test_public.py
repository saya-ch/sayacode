import unittest

from service import ParcelService
from store import ParcelStore


class PublicTests(unittest.TestCase):
    def test_dispatch(self):
        service = ParcelService(ParcelStore())
        service.register("T1", "Li", "Road 1")
        self.assertEqual(service.dispatch("T1").status, "in_transit")
