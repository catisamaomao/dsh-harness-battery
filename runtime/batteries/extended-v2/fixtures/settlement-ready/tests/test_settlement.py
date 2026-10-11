import unittest
from src.settlement import normalize, convert, RatesPending

class SettlementTest(unittest.TestCase):
    def test_sale(self):
        self.assertEqual(normalize([{"id":"t1","currency":"EUR","kind":"sale","amount_cents":100}]),
                         [{"id":"t1","currency":"EUR","net_cents":100}])
    def test_refund(self):
        self.assertEqual(normalize([{"id":"t1","currency":"EUR","kind":"refund","amount_cents":100}])[0]["net_cents"], -100)
    def test_pending_is_not_authorized(self):
        with self.assertRaises(RatesPending):
            convert([], {"status":"pending", "approval_id":None, "eur_per_unit":None})
