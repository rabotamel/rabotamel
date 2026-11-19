import os
import sys
import unittest
from decimal import Decimal as D

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import currency as c  # noqa: E402


class CurrencyTests(unittest.TestCase):
    def test_rate(self):
        self.assertEqual(c.to_candies(D("1"), c.CANDY), 1)
        self.assertEqual(c.to_candies(D("100"), c.RUB), 125)    # 1🍬 = 0,8 ₽
        self.assertEqual(c.to_candies(D("0.8"), c.RUB), 1)
        self.assertEqual(c.to_candies(D("1"), c.USD), 100)      # 1🍬 = 0,01 $
        self.assertEqual(c.to_candies(D("12.5"), c.USD), 1250)

    def test_rounding_is_half_up_without_float_errors(self):
        self.assertEqual(c.to_candies(D("1.2"), c.RUB), 2)      # 1,2 / 0,8 = 1,5 -> 2 (во float вышло бы 1)
        self.assertEqual(c.to_candies(D("1"), c.RUB), 1)        # 1,25 -> 1
        self.assertEqual(c.to_candies(D("0.01"), c.RUB), 0)

    def test_parse(self):
        self.assertEqual(c.parse_amount("10,5", c.USD), D("10.5"))
        self.assertEqual(c.parse_amount(" 1 000 ", c.CANDY), D("1000"))
        for raw, cur in (("", c.CANDY), ("abc", c.RUB), ("0", c.CANDY), ("-5", c.RUB), ("1.5", c.CANDY),
                         ("1.234", c.USD), ("nan", c.RUB), ("inf", c.USD), ("1", "eur")):
            with self.assertRaises(ValueError, msg=(raw, cur)):
                c.parse_amount(raw, cur)

    def test_convert_all(self):
        r = c.convert_all(D("100"), c.RUB)
        self.assertEqual((r[c.CANDY], r[c.RUB], r[c.USD]), (D("125"), D("100"), D("1.25")))
        r = c.convert_all(D("100"), c.CANDY)
        self.assertEqual((r[c.RUB], r[c.USD]), (D("80.0"), D("1.00")))

    def test_format_money(self):
        self.assertEqual(c.format_money(D("100")), "100")
        self.assertEqual(c.format_money(D("1.5")), "1,5")
        self.assertEqual(c.format_money(D("1234.56")), "1 234,56")
        self.assertEqual(c.format_money(D("0.005")), "0,01")


if __name__ == "__main__":
    unittest.main()
