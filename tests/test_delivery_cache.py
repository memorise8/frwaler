import unittest

from delivery.be.cache import TTLCache


class DeliveryCacheTest(unittest.TestCase):
    def test_reuses_value_and_returns_defensive_copy(self):
        calls = []
        cache = TTLCache(30)
        first = cache.get_or_create(
            "stats", lambda: calls.append(1) or {"documents": 1}
        )
        first["documents"] = 99
        second = cache.get_or_create(
            "stats", lambda: calls.append(2) or {"documents": 2}
        )
        self.assertEqual(second, {"documents": 1})
        self.assertEqual(calls, [1])

    def test_zero_ttl_refreshes(self):
        cache = TTLCache(0)
        calls = []
        cache.get_or_create("stats", lambda: calls.append(1) or 1)
        value = cache.get_or_create("stats", lambda: calls.append(2) or 2)
        self.assertEqual(value, 2)
        self.assertEqual(calls, [1, 2])


if __name__ == "__main__":
    unittest.main()
