"""Offline unit tests for calc fixture."""

from __future__ import annotations

import unittest

from calc import add, clamp, divide


class CalcTests(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-1, 1), 0)

    def test_divide(self) -> None:
        self.assertEqual(divide(10, 2), 5.0)
        with self.assertRaises(ZeroDivisionError):
            divide(10, 0)

    def test_clamp(self) -> None:
        self.assertEqual(clamp(5, 0, 10), 5)
        self.assertEqual(clamp(-3, 0, 10), 0)
        self.assertEqual(clamp(42, 0, 10), 10)


if __name__ == "__main__":
    unittest.main()
