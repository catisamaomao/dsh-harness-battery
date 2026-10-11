import unittest
from cache_config import load_settings

class SettingsTest(unittest.TestCase):
    def test_positive_capacity(self):
        self.assertGreater(load_settings()["max_entries"], 0)
    def test_supported_eviction(self):
        self.assertEqual(load_settings()["eviction"], "lru")
