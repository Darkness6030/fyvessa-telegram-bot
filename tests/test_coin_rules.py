import unittest
from decimal import Decimal

from src.coins import adjusted_coin_balance, manual_coin_adjustment, whole_coin_reward
from src.pricing import money, money_sum


class WholeCoinRewardTests(unittest.TestCase):
    def test_accepts_zero_and_unbounded_integers(self):
        huge = Decimal('10000000000000000000000000000000000000000')

        self.assertEqual(whole_coin_reward(Decimal('0')), Decimal('0'))
        self.assertEqual(whole_coin_reward(huge), huge)

    def test_rejects_fractional_negative_and_non_finite_values(self):
        for value in (
            Decimal('-1'),
            Decimal('1.5'),
            Decimal('NaN'),
            Decimal('Infinity'),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                whole_coin_reward(value)


class ManualCoinAdjustmentTests(unittest.TestCase):
    def test_accepts_credit_and_debit_with_two_decimal_places(self):
        self.assertEqual(manual_coin_adjustment(Decimal('12.34')), Decimal('12.34'))
        self.assertEqual(manual_coin_adjustment(Decimal('-5')), Decimal('-5.00'))

    def test_rejects_zero_excess_precision_and_non_finite_values(self):
        for value in (Decimal('0'), Decimal('1.001'), Decimal('NaN')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                manual_coin_adjustment(value)

    def test_adjusted_balance_cannot_be_negative(self):
        self.assertEqual(
            adjusted_coin_balance(Decimal('100'), Decimal('-25.50')),
            (Decimal('-25.50'), Decimal('74.50')),
        )
        with self.assertRaisesRegex(ValueError, 'отрицательным'):
            adjusted_coin_balance(Decimal('10'), Decimal('-10.01'))


class MoneyPrecisionTests(unittest.TestCase):
    def test_preserves_large_coin_values(self):
        huge = Decimal('10000000000000000000000000000000000000000')

        self.assertEqual(money(huge), Decimal(f'{huge}.00'))
        self.assertEqual(
            money_sum(huge, Decimal('5')),
            Decimal('10000000000000000000000000000000000000005.00'),
        )


if __name__ == '__main__':
    unittest.main()
