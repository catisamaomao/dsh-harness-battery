import unittest
from money import amount_to_cents

class MoneyTest(unittest.TestCase):
    def test_integer(self):
        self.assertEqual(amount_to_cents("12"), 1200)
    def test_half(self):
        self.assertEqual(amount_to_cents("1.5"), 150)
    def test_zero(self):
        self.assertEqual(amount_to_cents("0"), 0)
