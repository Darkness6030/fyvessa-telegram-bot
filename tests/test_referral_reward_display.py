import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jinja2 import Environment, FileSystemLoader, select_autoescape


fake_bot = types.ModuleType('src.bot')
fake_bot.get_bot = lambda: None
fake_bot.send_message = AsyncMock(return_value=True)
sys.modules.setdefault('src.bot', fake_bot)

from src.referrals import _approval_messages  # noqa: E402


class ReferralRewardTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        templates_directory = Path(__file__).resolve().parents[1] / 'templates'
        environment = Environment(
            loader=FileSystemLoader(templates_directory),
            autoescape=select_autoescape(('html',)),
        )
        cls.template = environment.get_template('_referrals_content.html')

    def render(self, referrer_reward: str, invitee_reward: str) -> str:
        channel = SimpleNamespace(
            id=1,
            platform='Telegram',
            account_name='Fyvessa',
            url='https://t.me/fyvessa',
            coin_reward=Decimal(referrer_reward),
            invitee_coin_reward=Decimal(invitee_reward),
            supports_automatic_check=True,
        )
        reward = SimpleNamespace(
            status='approved',
            invitee_reward_amount=Decimal(invitee_reward),
        )
        return self.template.render(
            user=SimpleNamespace(referrer_id=10),
            referral_link='https://t.me/bot?start=1',
            channels=[channel],
            rewards_by_channel={channel.id: reward},
            invited_count=1,
            approved_count=1,
            earned_coins=Decimal('0'),
            coin_transactions=[],
        )

    def test_hides_both_zero_reward_labels(self):
        rendered = self.render('0', '0')

        self.assertNotIn('За друга:', rendered)
        self.assertNotIn('За подписку:', rendered)
        self.assertNotIn('Вам начислено', rendered)
        self.assertIn('Подписка подтверждена ✓', rendered)

    def test_hides_only_zero_invitee_reward(self):
        rendered = self.render('5', '0')

        self.assertIn('За друга: +5', rendered)
        self.assertNotIn('За подписку:', rendered)
        self.assertNotIn('Вам начислено', rendered)

    def test_hides_only_zero_referrer_reward(self):
        rendered = self.render('0', '3')

        self.assertNotIn('За друга:', rendered)
        self.assertIn('За подписку: +3', rendered)
        self.assertIn('Вам начислено 3 коинов', rendered)


class ReferralApprovalMessageTests(unittest.TestCase):
    channel = SimpleNamespace(platform='Telegram', account_name='Fyvessa & Co')

    def test_zero_rewards_are_omitted_from_messages(self):
        referrer_message, invitee_message = _approval_messages(
            self.channel,
            Decimal('0'),
            Decimal('0'),
            Decimal('1.00'),
        )

        self.assertNotIn('0 коинов', referrer_message)
        self.assertNotIn('0 коинов', invitee_message)
        self.assertNotIn('Вам начислено', invitee_message)
        self.assertIn('Персональная скидка: 1.00%.', referrer_message)
        self.assertIn('Fyvessa &amp; Co', invitee_message)

    def test_each_positive_reward_is_shown_independently(self):
        referrer_message, invitee_message = _approval_messages(
            self.channel,
            Decimal('5'),
            Decimal('0'),
            Decimal('1'),
        )
        self.assertIn('5 коинов', referrer_message)
        self.assertNotIn('Вам начислено', invitee_message)

        referrer_message, invitee_message = _approval_messages(
            self.channel,
            Decimal('0'),
            Decimal('3'),
            Decimal('1'),
        )
        self.assertNotIn('0 коинов', referrer_message)
        self.assertIn('Вам начислено: 3 коинов', invitee_message)


if __name__ == '__main__':
    unittest.main()
