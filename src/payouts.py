from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal
from zoneinfo import ZoneInfo

import anyio
from pydantic import BaseModel, Field
from rewire import config, logger, simple_plugin
from rewire_sqlmodel import session_context, transaction

from src.bot import send_message
from src.models import AppSetting, Order, PartnerPayout, Promocode
from src.pricing import money

LAST_CUTOFF_KEY = 'partner_payout_last_cutoff'


@config
class Config(BaseModel):
    admin_chat_id: int
    payout_hour: int = Field(default=12, ge=0, le=23)
    payout_timezone: str = 'Europe/Moscow'


@dataclass(frozen=True)
class PartnerAccrual:
    promocode: Promocode
    orders_count: int
    orders_total: Decimal
    reward_percent: Decimal
    payout_amount: Decimal


plugin = simple_plugin()


def _timezone() -> tzinfo:
    try:
        return ZoneInfo(Config.payout_timezone)
    except Exception:
        logger.error(
            'Unknown payout timezone {}, using Europe/Moscow',
            Config.payout_timezone,
        )
        return timezone(timedelta(hours=3), name='Europe/Moscow')


def _local_now(now: datetime | None = None) -> datetime:
    timezone = _timezone()
    if now is None:
        return datetime.now(timezone)

    if now.tzinfo:
        return now.astimezone(timezone)

    return now.replace(tzinfo=timezone)


def latest_payout_cutoff(now: datetime | None = None) -> datetime:
    timezone = _timezone()
    local_now = _local_now(now)
    days_since_friday = (local_now.weekday() - 4) % 7
    friday = local_now.date() - timedelta(days=days_since_friday)
    candidate = datetime.combine(friday, time(Config.payout_hour), timezone)
    if candidate > local_now:
        candidate -= timedelta(days=7)
    return candidate.replace(tzinfo=None)


def next_payout_cutoff(now: datetime | None = None) -> datetime:
    timezone = _timezone()
    local_now = _local_now(now)
    days_until_friday = (4 - local_now.weekday()) % 7
    friday = local_now.date() + timedelta(days=days_until_friday)
    candidate = datetime.combine(friday, time(Config.payout_hour), timezone)
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate.replace(tzinfo=None)


def payout_cutoff_on_or_after(moment: datetime) -> datetime:
    timezone = _timezone()
    local_moment = _local_now(moment)
    days_until_friday = (4 - local_moment.weekday()) % 7
    friday = local_moment.date() + timedelta(days=days_until_friday)
    candidate = datetime.combine(friday, time(Config.payout_hour), timezone)
    if candidate < local_moment:
        candidate += timedelta(days=7)
    return candidate.replace(tzinfo=None)


async def _eligible_orders(cutoff: datetime | None = None) -> list[Order]:
    return [
        order
        for order in await Order.get_all()
        if order.payment_status == 'paid'
           and order.partner_reward > 0
           and order.promo_code_id is not None
           and order.partner_payout_id is None
           and (cutoff is None or bool(order.paid_at and order.paid_at <= cutoff))
    ]


async def current_partner_accruals() -> list[PartnerAccrual]:
    promocodes = {
        promocode.id: promocode
        for promocode in await Promocode.get_all()
        if not promocode.is_deleted
    }
    grouped: dict[int, list[Order]] = defaultdict(list)
    for order in await _eligible_orders():
        if order.promo_code_id in promocodes:
            grouped[order.promo_code_id].append(order)

    result = []
    for promocode_id in promocodes:
        orders = grouped.get(promocode_id, [])
        orders_total = money(sum(
            (order.paid_total for order in orders),
            Decimal('0'),
        ))
        payout_amount = money(sum(
            (order.partner_reward for order in orders),
            Decimal('0'),
        ))
        reward_percent = (
            money(payout_amount * Decimal('100') / orders_total)
            if orders_total else promocodes[promocode_id].partner_reward_percent
        )
        result.append(PartnerAccrual(
            promocode=promocodes[promocode_id],
            orders_count=len(orders),
            orders_total=orders_total,
            reward_percent=reward_percent,
            payout_amount=payout_amount,
        ))
    return result


@transaction(1)
async def create_due_payouts(cutoff: datetime | None = None) -> list[PartnerPayout]:
    target_cutoff = cutoff or latest_payout_cutoff()
    state = await AppSetting.get_by_key(LAST_CUTOFF_KEY)
    if state:
        try:
            last_cutoff = datetime.fromisoformat(state.value)
        except ValueError:
            last_cutoff = None
        if last_cutoff and last_cutoff >= target_cutoff:
            return []
    else:
        last_cutoff = None

    promocodes = {
        promocode.id: promocode
        for promocode in await Promocode.get_all()
    }
    session = session_context.get()
    payouts = []
    if last_cutoff:
        next_cutoff = last_cutoff + timedelta(days=7)
    else:
        initial_orders = await _eligible_orders(target_cutoff)
        earliest_paid_at = min(
            (order.paid_at for order in initial_orders if order.paid_at),
            default=None,
        )
        next_cutoff = (
            payout_cutoff_on_or_after(earliest_paid_at)
            if earliest_paid_at else target_cutoff
        )

    while next_cutoff <= target_cutoff:
        eligible = await _eligible_orders(next_cutoff)
        grouped: dict[int, list[Order]] = defaultdict(list)
        for order in eligible:
            grouped[order.promo_code_id].append(order)

        for promocode_id, orders in grouped.items():
            promocode = promocodes.get(promocode_id)
            if not promocode:
                continue
            period_start = last_cutoff or min(
                (order.paid_at for order in orders if order.paid_at),
                default=next_cutoff - timedelta(days=7),
            )
            orders_total = money(sum((order.paid_total for order in orders), Decimal('0')))
            payout_amount = money(sum((order.partner_reward for order in orders), Decimal('0')))
            reward_percent = money(
                payout_amount * Decimal('100') / orders_total,
            ) if orders_total else promocode.partner_reward_percent
            payout = PartnerPayout(
                period_started_at=period_start,
                period_ended_at=next_cutoff,
                promo_code_id=promocode.id,
                partner_name_snapshot=promocode.partner_name,
                promo_code_snapshot=promocode.code,
                reward_percent_snapshot=reward_percent,
                orders_count=len(orders),
                orders_total=orders_total,
                payout_amount=payout_amount,
            ).add()
            await session.flush()
            for order in orders:
                order.partner_payout_id = payout.id
                order.add()
            payouts.append(payout)
        last_cutoff = next_cutoff
        next_cutoff += timedelta(days=7)

    if not state:
        state = AppSetting(key=LAST_CUTOFF_KEY)
    state.value = target_cutoff.isoformat()
    state.updated_at = datetime.now()
    state.add()
    return payouts


async def mark_payout_paid(payout: PartnerPayout, admin_id: int) -> bool:
    payout = await PartnerPayout.get_by_id_for_update(payout.id) or payout
    if payout.status == 'paid':
        return False
    if payout.status != 'pending':
        raise ValueError('Неизвестный статус выплаты')
    payout.status = 'paid'
    payout.paid_at = datetime.now()
    payout.paid_by_admin_id = admin_id
    payout.add()
    return True


@plugin.run()
async def payout_scheduler() -> None:
    while True:
        try:
            payouts = await create_due_payouts()
            if payouts:
                total = money(sum((payout.payout_amount for payout in payouts), Decimal('0')))
                await send_message(
                    Config.admin_chat_id,
                    '<b>Сформированы пятничные выплаты партнёрам</b>\n\n'
                    f'Записей: {len(payouts)}\n'
                    f'Итого: {total} ₽\n\n'
                    'Откройте /admin → «Выплаты партнёрам».',
                )
        except Exception as exc:
            logger.opt(exception=True).error('Partner payout scheduler failed: {}', exc)
        await anyio.sleep(60)
