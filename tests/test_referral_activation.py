import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch


fake_bot = types.ModuleType('src.bot')
fake_bot.get_bot = lambda: None
fake_bot.send_message = AsyncMock(return_value=True)
sys.modules.setdefault('src.bot', fake_bot)

from src import referrals  # noqa: E402
from src.models import CoinTransaction, User  # noqa: E402


class ReferralActivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_activation_is_awarded_to_referrer_only_once(self):
        referrer = User(id=1, coin_balance=Decimal('10'))
        invited = User(id=2, referrer_id=referrer.id)
        transactions = []

        with (
            patch.object(
                User,
                'get_by_id_for_update',
                AsyncMock(return_value=referrer),
            ) as get_referrer,
            patch.object(User, 'add', lambda instance: instance),
            patch.object(
                CoinTransaction,
                'add',
                lambda transaction: transactions.append(transaction) or transaction,
            ),
            patch.object(
                referrals,
                'get_referral_activation_reward',
                AsyncMock(return_value=Decimal('5')),
            ),
        ):
            first_reward = await referrals.award_referral_activation(invited)
            second_reward = await referrals.award_referral_activation(invited)

        self.assertEqual(first_reward, Decimal('5'))
        self.assertEqual(second_reward, Decimal('5'))
        self.assertEqual(referrer.coin_balance, Decimal('15.00'))
        self.assertEqual(invited.coin_balance, Decimal('0'))
        self.assertIsNotNone(invited.referral_activation_reward_awarded_at)
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].user_id, referrer.id)
        self.assertIn(str(invited.id), transactions[0].reason)
        get_referrer.assert_awaited_once_with(referrer.id)

    async def test_zero_reward_is_marked_as_processed(self):
        referrer = User(id=1, coin_balance=Decimal('10'))
        invited = User(id=2, referrer_id=referrer.id)

        with (
            patch.object(
                User,
                'get_by_id_for_update',
                AsyncMock(return_value=referrer),
            ),
            patch.object(User, 'add', lambda instance: instance),
            patch.object(CoinTransaction, 'add') as add_transaction,
            patch.object(
                referrals,
                'get_referral_activation_reward',
                AsyncMock(return_value=Decimal('0')),
            ),
        ):
            reward = await referrals.award_referral_activation(invited)

        self.assertEqual(reward, Decimal('0'))
        self.assertIsNotNone(invited.referral_activation_reward_awarded_at)
        self.assertEqual(referrer.coin_balance, Decimal('10'))
        add_transaction.assert_not_called()


class ManualCoinServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_adjustment_is_audited(self):
        user = User(id=7, coin_balance=Decimal('20'))
        transactions = []

        with (
            patch.object(
                User,
                'get_by_id_for_update',
                AsyncMock(return_value=user),
            ),
            patch.object(User, 'add', lambda instance: instance),
            patch.object(
                CoinTransaction,
                'add',
                lambda transaction: transactions.append(transaction) or transaction,
            ),
        ):
            transaction = await referrals.adjust_user_coins(
                user,
                Decimal('-3.25'),
                'Исправление операции',
                admin_id=99,
            )

        self.assertEqual(user.coin_balance, Decimal('16.75'))
        self.assertEqual(transaction.amount, Decimal('-3.25'))
        self.assertEqual(transaction.balance_after, Decimal('16.75'))
        self.assertEqual(transaction.admin_id, 99)
        self.assertEqual(len(transactions), 1)


if __name__ == '__main__':
    unittest.main()
