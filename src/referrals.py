import html
from datetime import datetime
from decimal import Decimal, InvalidOperation

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from fastapi import HTTPException
from rewire_sqlmodel import session_context

from src.bot import get_bot, send_message
from src.coins import adjusted_coin_balance, whole_coin_reward
from src.models import (
    AppSetting,
    CoinTransaction,
    Order,
    ReferralReward,
    SocialChannel,
    TelegramJoinRequest,
    User,
)
from src.pricing import money, money_sum

PURCHASE_COIN_PERCENT_KEY = 'purchase_coin_percent'
REFERRAL_ACTIVATION_REWARD_KEY = 'referral_activation_coin_reward'
DEFAULT_REFERRAL_ACTIVATION_REWARD = Decimal('5')
MAX_REFERRAL_DISCOUNT = Decimal('10')
REFERRAL_DISCOUNT_STEP = Decimal('1')
_bot_username = ''


def _approval_messages(
    channel: SocialChannel,
    reward_amount: Decimal,
    invitee_reward_amount: Decimal,
    personal_discount_percent: Decimal,
) -> tuple[str, str]:
    referrer_message = (
        (
            '<b>Реферальная награда начислена</b> 🎉\n\n'
            if reward_amount
            else '<b>Подписка приглашённого подтверждена</b> 🎉\n\n'
        )
        + (
            f'{reward_amount} коинов за подтверждённую подписку приглашённого.\n'
            if reward_amount
            else ''
        )
        + f'Персональная скидка: {personal_discount_percent}%.'
    )
    invitee_message = (
        '<b>Подписка подтверждена</b> 🎉\n\n'
        f'{html.escape(channel.account_name)}.'
        + (
            f'\nВам начислено: {invitee_reward_amount} коинов.'
            if invitee_reward_amount
            else ''
        )
    )
    return referrer_message, invitee_message


async def get_purchase_coin_percent() -> Decimal:
    setting = await AppSetting.get_by_key(PURCHASE_COIN_PERCENT_KEY)
    if not setting:
        return Decimal('0')
    try:
        value = Decimal(setting.value)
    except InvalidOperation:
        return Decimal('0')
    return min(max(value, Decimal('0')), Decimal('100'))


async def set_purchase_coin_percent(value: Decimal) -> Decimal:
    if not Decimal('0') <= value <= Decimal('100'):
        raise ValueError('Процент должен быть от 0 до 100')
    value = money(value)
    setting = await AppSetting.get_by_key(PURCHASE_COIN_PERCENT_KEY)
    if not setting:
        setting = AppSetting(key=PURCHASE_COIN_PERCENT_KEY)
    setting.value = str(value)
    setting.updated_at = datetime.now()
    setting.add()
    return value


async def get_referral_activation_reward() -> Decimal:
    setting = await AppSetting.get_by_key(REFERRAL_ACTIVATION_REWARD_KEY)
    if not setting:
        return DEFAULT_REFERRAL_ACTIVATION_REWARD
    try:
        return whole_coin_reward(Decimal(setting.value))
    except (InvalidOperation, ValueError):
        return DEFAULT_REFERRAL_ACTIVATION_REWARD


async def set_referral_activation_reward(value: Decimal) -> Decimal:
    value = whole_coin_reward(value)
    setting = await AppSetting.get_by_key(REFERRAL_ACTIVATION_REWARD_KEY)
    if not setting:
        setting = AppSetting(key=REFERRAL_ACTIVATION_REWARD_KEY)
    setting.value = str(value)
    setting.updated_at = datetime.now()
    setting.add()
    return value


async def award_referral_activation(user: User) -> Decimal:
    if (
        not user.referrer_id
        or user.referrer_id == user.id
        or user.referral_activation_reward_awarded_at is not None
    ):
        return user.referral_activation_reward_amount

    referrer = await User.get_by_id_for_update(user.referrer_id)
    if not referrer:
        return Decimal('0')

    reward = await get_referral_activation_reward()
    current_time = datetime.now()
    user.referral_activation_reward_awarded_at = current_time
    user.referral_activation_reward_amount = reward
    user.updated_at = current_time
    user.add()

    if not reward:
        return reward

    referrer.coin_balance = money_sum(referrer.coin_balance, reward)
    referrer.updated_at = current_time
    referrer.add()
    CoinTransaction(
        user_id=referrer.id,
        amount=reward,
        balance_after=referrer.coin_balance,
        reason=f'Активация бота приглашённым пользователем {user.id}',
    ).add()
    return reward


async def adjust_user_coins(
    user: User,
    amount: Decimal,
    reason: str,
    admin_id: int,
) -> CoinTransaction:
    reason = reason.strip()
    if not reason or len(reason) > 300:
        raise ValueError('Укажите причину длиной до 300 символов')

    user = await User.get_by_id_for_update(user.id) or user
    amount, balance_after = adjusted_coin_balance(user.coin_balance, amount)

    user.coin_balance = balance_after
    user.updated_at = datetime.now()
    user.add()
    transaction = CoinTransaction(
        user_id=user.id,
        admin_id=admin_id,
        amount=amount,
        balance_after=balance_after,
        reason=f'Ручная корректировка: {reason}',
    ).add()
    return transaction


async def bot_username() -> str:
    global _bot_username
    if not _bot_username:
        try:
            me = await get_bot().get_me()
            _bot_username = me.username or ''
        except TelegramAPIError:
            return ''
    return _bot_username


async def personal_referral_link(user_id: int) -> str:
    username = await bot_username()
    return f'https://t.me/{username}?start={user_id}' if username else ''


async def _telegram_member(channel: SocialChannel, user_id: int) -> bool:
    if not channel.telegram_chat_id:
        return False

    join_request = await TelegramJoinRequest.get_for_user_channel(user_id, channel.id)
    try:
        member = await get_bot().get_chat_member(channel.telegram_chat_id, user_id)
    except TelegramAPIError:
        if join_request:
            return True
        raise
    if member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    }:
        return True

    return (
        member.status == ChatMemberStatus.RESTRICTED and member.is_member
    ) or join_request is not None


async def initialize_referral_rewards(user: User) -> list[ReferralReward]:
    referrer_id = None
    if (
        user.referrer_id
        and user.referrer_id != user.id
        and await User.get_by_id(user.referrer_id)
    ):
        referrer_id = user.referrer_id

    rewards = []
    for channel in await SocialChannel.get_active():
        reward = await ReferralReward.get_for_user_channel(user.id, channel.id)
        if reward:
            rewards.append(reward)
            continue

        status = 'pending'
        if channel.supports_automatic_check:
            try:
                if await _telegram_member(channel, user.id):
                    status = 'preexisting'
            except TelegramAPIError:
                # Leave the action available: the admin can fix bot rights and retry.
                pass

        reward = ReferralReward(
            invited_user_id=user.id,
            referrer_id=referrer_id,
            social_channel_id=channel.id,
            status=status,
        ).add()
        rewards.append(reward)
    await session_context.get().flush()
    return rewards


async def approve_referral_reward(reward: ReferralReward, admin_id: int | None = None) -> bool:
    reward = await ReferralReward.get_by_id_for_update(reward.id) or reward
    if reward.status == 'approved':
        return False

    if reward.status == 'preexisting':
        raise ValueError('Пользователь уже был подписан до участия в программе')

    invited_user = await User.get_by_id(reward.invited_user_id)
    referrer = (
        await User.get_by_id(reward.referrer_id)
        if reward.referrer_id
        else None
    )
    channel = await SocialChannel.get_by_id(reward.social_channel_id)
    if not invited_user or not channel or (reward.referrer_id and not referrer):
        raise ValueError('Реферальные данные больше недоступны')

    if referrer and invited_user.id == referrer.id:
        raise ValueError('Нельзя пригласить самого себя')

    rewards_were_snapshotted = reward.verified_at is not None
    reward_amount = money(
        (reward.reward_amount if rewards_were_snapshotted else channel.coin_reward)
        if referrer
        else Decimal('0')
    )
    invitee_reward_amount = money(
        reward.invitee_reward_amount
        if rewards_were_snapshotted
        else channel.invitee_coin_reward
    )

    reward.status = 'approved'
    reward.reward_amount = reward_amount
    reward.invitee_reward_amount = invitee_reward_amount
    current_time = datetime.now()
    reward.verified_at = current_time
    reward.reviewed_by_admin_id = admin_id
    reward.add()
    await session_context.get().flush()

    if referrer and reward_amount:
        referrer.coin_balance = money_sum(referrer.coin_balance, reward_amount)
        referrer.updated_at = current_time
        referrer.add()
        CoinTransaction(
            user_id=referrer.id,
            social_channel_id=channel.id,
            referral_reward_id=reward.id,
            amount=reward_amount,
            balance_after=referrer.coin_balance,
            reason=(
                f'Подписка приглашённого {invited_user.id}: '
                f'{channel.account_name}'
            ),
        ).add()

    if invitee_reward_amount:
        invited_user.coin_balance = money_sum(
            invited_user.coin_balance, invitee_reward_amount
        )
        invited_user.updated_at = current_time
        invited_user.add()
        CoinTransaction(
            user_id=invited_user.id,
            social_channel_id=channel.id,
            referral_reward_id=reward.id,
            amount=invitee_reward_amount,
            balance_after=invited_user.coin_balance,
            reason=f'Награда за подписку: {channel.account_name}',
        ).add()

    if referrer and invited_user.referral_discount_awarded_at is None:
        referrer.personal_discount_percent = min(
            MAX_REFERRAL_DISCOUNT,
            money(referrer.personal_discount_percent + REFERRAL_DISCOUNT_STEP),
        )
        referrer.updated_at = current_time
        referrer.add()
        invited_user.referral_discount_awarded_at = current_time
        invited_user.add()
    referrer_message, invitee_message = _approval_messages(
        channel,
        reward_amount,
        invitee_reward_amount,
        referrer.personal_discount_percent if referrer else Decimal('0'),
    )
    if referrer:
        await send_message(referrer.id, referrer_message)
    await send_message(invited_user.id, invitee_message)
    return True


async def claim_referral_reward(
    user: User,
    channel: SocialChannel,
) -> ReferralReward:
    rewards = await initialize_referral_rewards(user)
    reward = next(
        (item for item in rewards if item.social_channel_id == channel.id),
        None,
    )

    if not reward:
        raise HTTPException(status_code=404, detail='Реферальное действие не найдено')

    if reward.status == 'approved':
        return reward

    if reward.status == 'preexisting':
        raise HTTPException(
            status_code=409,
            detail='Подписка существовала до открытия реферального задания',
        )

    if reward.status == 'review':
        return reward

    if channel.supports_automatic_check:
        try:
            is_member = await _telegram_member(channel, user.id)
        except TelegramAPIError as exc:
            raise HTTPException(
                status_code=503,
                detail='Проверка Telegram временно недоступна',
            ) from exc

        if not is_member:
            raise HTTPException(
                status_code=409,
                detail='Подписка пока не найдена. Подпишитесь и повторите проверку',
            )
        reward.reward_amount = money(
            channel.coin_reward if reward.referrer_id else Decimal('0')
        )
        reward.invitee_reward_amount = money(channel.invitee_coin_reward)
        reward.verified_at = datetime.now()
        reward.add()
        await approve_referral_reward(reward)
    else:
        reward.status = 'review'
        reward.reward_amount = money(
            channel.coin_reward if reward.referrer_id else Decimal('0')
        )
        reward.invitee_reward_amount = money(channel.invitee_coin_reward)
        reward.verified_at = datetime.now()
        reward.add()
    return reward


async def reject_referral_reward(reward: ReferralReward, admin_id: int) -> bool:
    reward = await ReferralReward.get_by_id_for_update(reward.id) or reward
    if reward.status != 'review':
        return False
    reward.status = 'pending'
    reward.reward_amount = Decimal('0')
    reward.invitee_reward_amount = Decimal('0')
    reward.verified_at = None
    reward.reviewed_by_admin_id = admin_id
    reward.add()
    return True


async def award_purchase_coins(user: User, order: Order) -> Decimal:
    if order.purchase_coins_awarded:
        return order.purchase_coins_awarded
    percent = await get_purchase_coin_percent()
    reward = money(order.paid_total * percent / Decimal('100'))
    order.purchase_coin_percent = percent
    order.purchase_coins_awarded = reward
    order.add()
    if not reward:
        return reward

    user.coin_balance = money_sum(user.coin_balance, reward)
    user.updated_at = datetime.now()
    user.add()
    CoinTransaction(
        user_id=user.id,
        order_id=order.id,
        amount=reward,
        balance_after=user.coin_balance,
        reason=f'Коины за оплаченный заказ {order.number} ({percent}%)',
    ).add()
    return reward
