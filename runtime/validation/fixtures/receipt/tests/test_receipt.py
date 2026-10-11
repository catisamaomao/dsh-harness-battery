import unittest
from receipt import render_receipt

class ReceiptTest(unittest.TestCase):
    def test_old_output(self):
        self.assertEqual(render_receipt([{"name": "Tea", "cents": 300}]), "Tea: 300\nTotal: 300")
    def test_empty(self):
        self.assertEqual(render_receipt([]), "Total: 0")
