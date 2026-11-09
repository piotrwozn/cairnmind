import unittest

from checkout import total_cents


class CheckoutTests(unittest.TestCase):
    def test_exact_total(self):
        self.assertEqual(total_cents([1999, 599, 250]), 2848)

    def test_empty_cart(self):
        self.assertEqual(total_cents([]), 0)

    def test_invalid_prices(self):
        for price in (-1, 19.99, True):
            with self.subTest(price=price), self.assertRaises(ValueError):
                total_cents([price])


if __name__ == "__main__":
    unittest.main(verbosity=2)
