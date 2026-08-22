import html
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, WebAppInfo
from pydantic import BaseModel
from rewire import config, simple_plugin
from rewire_sqlmodel import session_context, transaction

from src import bot
from src.catalog import CatalogValidationError, sync_catalog
from src.coins import whole_coin_reward
from src.keyboards import inline_keyboard
from src.models import (
    AvailabilityRequest,
    Banner,
    CoinTransaction,
    Order,
    PartnerPayout,
    Product,
    Promocode,
    ReferralReward,
    SocialChannel,
    User,
)
from src.orders import cancel_order, confirm_payment, DELIVERY_METHOD_LABELS, ORDER_STATUS_LABELS, SHIPPING_STATUS_LABELS, update_shipping_status
from src.payouts import current_partner_accruals, mark_payout_paid, next_payout_cutoff
from src.referrals import (
    adjust_user_coins,
    approve_referral_reward,
    get_referral_activation_reward,
    get_purchase_coin_percent,
    reject_referral_reward,
    set_referral_activation_reward,
    set_purchase_coin_percent,
)
from src.settings import SettingsValidationError, sync_settings


@config
class Config(BaseModel):
    admin_chat_id: int
    mini_app_url: str


plugin = simple_plugin()
router = Router(name='admin')

router.message.filter(F.chat.id == Config.admin_chat_id)
router.callback_query.filter(F.message.chat.id == Config.admin_chat_id)


class AdminSectionCallback(CallbackData, prefix='adm'):
    section: str
    page: int = 0


class AvailabilityActionCallback(CallbackData, prefix='av'):
    request_id: int
    status: str


class OrderActionCallback(CallbackData, prefix='ord'):
    order_id: int
    action: str


class UserSectionCallback(CallbackData, prefix='usr'):
    user_id: int
    section: str
    page: int = 0


class PromocodeToggleCallback(CallbackData, prefix='promo'):
    promocode_id: int


class PromocodeActionCallback(CallbackData, prefix='pma'):
    action: str
    promocode_id: int = 0
    page: int = 0


class PayoutActionCallback(CallbackData, prefix='pay'):
    action: str
    payout_id: int = 0
    page: int = 0


class BannerActionCallback(CallbackData, prefix='bnr'):
    action: str
    banner_id: int = 0
    page: int = 0


class SocialActionCallback(CallbackData, prefix='soc'):
    action: str
    channel_id: int = 0
    page: int = 0


class ReferralReviewCallback(CallbackData, prefix='ref'):
    action: str
    reward_id: int
    page: int = 0


class CoinSettingsCallback(CallbackData, prefix='coin'):
    action: str
    page: int = 0
    user_id: int = 0


class PromocodeForm(StatesGroup):
    details = State()


class BannerForm(StatesGroup):
    details = State()


class SocialForm(StatesGroup):
    details = State()


class CoinSettingsForm(StatesGroup):
    percent = State()
    activation_reward = State()
    adjustment = State()


AVAILABILITY_LABELS = {
    'pending': 'Ожидает ответа',
    'available': 'Есть в наличии',
    'unavailable': 'Нет в наличии',
    'on_request': 'Под заказ / уточняется',
    'used': 'Использовано в заказе',
}

AVAILABILITY_COMMAND_STATUSES = {
    'available': 'available',
    'есть': 'available',
    'unavailable': 'unavailable',
    'нет': 'unavailable',
    'on_request': 'on_request',
    'уточняется': 'on_request',
}

ORDER_COMMAND_ACTIONS = {
    'paid': 'paid',
    'оплачен': 'paid',
    'cancel': 'cancel',
    'отмена': 'cancel',
    'assembling': 'assembling',
    'собирается': 'assembling',
    'shipped': 'shipped',
    'отправлен': 'shipped',
    'delivered': 'delivered',
    'доставлен': 'delivered',
}

BANNER_DIRECTORY = Path('assets/banners')
COIN_HISTORY_PAGE_SIZE = 7
BANNER_IMAGE_SUFFIXES = {
    'image/avif': '.avif',
    'image/gif': '.gif',
    'image/jpeg': '.jpg',
    'image/jpg': '.jpg',
    'image/png': '.png',
    'image/webp': '.webp',
}


def _cart_keyboard():
    return inline_keyboard([
        (
            '🛒 Перейти в корзину',
            WebAppInfo(url=f'{Config.mini_app_url.rstrip('/')}/cart'),
        ),
    ])


def _availability_keyboard(request_id: int):
    buttons = [
        (
            button_text,
            AvailabilityActionCallback(request_id=request_id, status=status),
        )
        for button_text, status in (
            ('✅ Есть всё', 'available'),
            ('❌ Нет', 'unavailable'),
            ('⏳ Уточняется', 'on_request'),
        )
    ]

    buttons.append(('🏠 Меню', AdminSectionCallback(section='menu')))
    return inline_keyboard(buttons, 2, 1, 1)


async def notify_availability_request(
    availability: AvailabilityRequest,
    product: Product,
    user: User,
) -> bool:
    username = f'@{html.escape(user.username)}' if user.username else 'без username'
    return await bot.send_message(
        Config.admin_chat_id,
        f'<b>Новый запрос наличия №{availability.id}</b>\n\n'
        f'Товар: <b>{html.escape(product.name)}</b>\n'
        f'SKU: <code>{html.escape(product.sku)}</code>\n'
        f'Нужно: <b>{availability.requested_quantity or 1} шт.</b>\n'
        f'Покупатель: {user.id} ({username})',
        reply_markup=_availability_keyboard(availability.id),
    )


async def notify_payment_review(order: Order, user: User) -> bool:
    username = f'@{html.escape(user.username)}' if user.username else 'без username'
    return await bot.send_message(
        Config.admin_chat_id,
        f'<b>Покупатель сообщил об оплате</b>\n\n'
        f'Заказ: <b>{html.escape(order.number)}</b>\n'
        f'Сумма: <b>{order.paid_total} ₽</b>\n'
        f'Покупатель: {user.id} ({username})\n'
        f'Получатель: {html.escape(order.recipient_first_name)} '
        f'{html.escape(order.recipient_last_name)}, {html.escape(order.recipient_phone_number)}\n'
        f'{DELIVERY_METHOD_LABELS.get(order.delivery_method, order.delivery_method)}: '
        f'{html.escape(order.pickup_point_address)}',
        reply_markup=inline_keyboard([
            (
                '✅ Подтвердить оплату',
                OrderActionCallback(order_id=order.id, action='paid'),
            ),
            (
                '✖️ Отменить заказ',
                OrderActionCallback(order_id=order.id, action='cancel'),
            ),
            ('🏠 Меню', AdminSectionCallback(section='menu')),
        ], 1, 1, 1),
    )


async def notify_referral_review(
    reward: ReferralReward,
    channel: SocialChannel,
    user: User,
) -> bool:
    return await bot.send_message(
        Config.admin_chat_id,
        '<b>Подписка ожидает ручной проверки</b>\n\n'
        f'Пользователь: <code>{user.id}</code>\n'
        f'Площадка: {html.escape(channel.platform)} / '
        f'{html.escape(channel.account_name)}\n'
        f'Ссылка: {html.escape(channel.url)}\n'
        f'Награда пригласившему: {reward.reward_amount} коинов\n'
        f'Награда подписавшемуся: {reward.invitee_reward_amount} коинов',
        reply_markup=inline_keyboard([
            (
                '✅ Подтвердить',
                ReferralReviewCallback(action='approve', reward_id=reward.id),
            ),
            (
                '↩️ Вернуть',
                ReferralReviewCallback(action='reject', reward_id=reward.id),
            ),
            ('🏠 Меню', AdminSectionCallback(section='menu')),
        ], 2, 1),
    )


ADMIN_TEXT = (
    '<b>Администрирование Fyvessa</b>\n\n'
    'Выберите подраздел. Товары редактируются в Google Sheets, остальные операции '
    'выполняются здесь.'
)

ADMIN_GROUPS = {
    'admin_sales': (
        '🛒 Продажи',
        'Заказы, оплаты и запросы покупателей по наличию.',
        (
            ('💳 Заказы и оплаты', 'orders'),
            ('📦 Запросы наличия', 'availability'),
        ),
    ),
    'admin_clients': (
        '👥 Клиенты и лояльность',
        'Пользователи, реферальные проверки и настройки коинов.',
        (
            ('👥 Пользователи', 'users'),
            ('🤝 Реферальные проверки', 'referrals'),
            ('🪙 Настройки коинов', 'coins'),
        ),
    ),
    'admin_partners': (
        '🤝 Партнёры и выплаты',
        'Промокоды партнёров, текущие начисления и архив выплат.',
        (
            ('🎟 Промокоды', 'promos'),
            ('💸 Выплаты партнёрам', 'payouts'),
        ),
    ),
    'admin_content': (
        '🖼 Витрина и каналы',
        'Рекламные карточки на главной и площадки для подписок.',
        (
            ('🖼 Рекламные карточки', 'banners'),
            ('🌐 Социальные сети', 'socials'),
        ),
    ),
    'admin_system': (
        '⚙️ Система и отчёты',
        'Общая сводка, синхронизация Google Sheets и подсказки.',
        (
            ('📊 Сводка', 'summary'),
            ('🔄 Синхронизировать таблицу', 'sync'),
            ('❓ Команды и подсказки', 'help'),
        ),
    ),
}

SYNC_STARTED_TEXT = (
    '🔄 <b>Синхронизация запущена</b>\n\n'
    'Загружаем таблицу, товары и изображения. Это может занять несколько минут. '
    'Пожалуйста, дождитесь итогового сообщения и не запускайте синхронизацию повторно.'
)


async def _edit_message(callback: CallbackQuery, text: str, reply_markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if 'message is not modified' not in str(exc):
            raise


def _admin_keyboard():
    return inline_keyboard([
        (group[0], AdminSectionCallback(section=section))
        for section, group in ADMIN_GROUPS.items()
    ])


def _admin_group_text(section: str) -> str:
    title, description, _ = ADMIN_GROUPS[section]
    return f'<b>{title}</b>\n\n{description}'


def _admin_group_keyboard(section: str):
    _, _, actions = ADMIN_GROUPS[section]
    return inline_keyboard([
        *(
            (text, AdminSectionCallback(section=action))
            for text, action in actions
        ),
        ('🏠 Главное меню', AdminSectionCallback(section='menu')),
    ])


def _back_keyboard(section: str = 'menu', page: int = 0):
    if section == 'menu':
        return inline_keyboard([
            ('🏠 Меню', AdminSectionCallback(section='menu')),
        ])

    section_labels = {
        'availability': '← К запросам',
        'orders': '← К заказам',
        'promos': '← К промокодам',
        'users': '← К пользователям',
        'payout_current': '← К начислениям',
        'payout_pending': '← К ожидающим выплатам',
        'payout_archive': '← К архиву',
        'banners': '← К карточкам',
        'socials': '← К соцсетям',
        'referrals': '← К проверкам',
        'coins': '← К коинам',
    }

    return inline_keyboard([
        (
            section_labels.get(section, '← К разделу'),
            AdminSectionCallback(section=section, page=page),
        ),
        ('🏠 Меню', AdminSectionCallback(section='menu')),
    ])


def _rubles(value: Decimal) -> str:
    value = value.quantize(Decimal('0.01'))
    formatted = f'{value:,.2f}'.replace(',', ' ').replace('.', ',')
    return f'{formatted.removesuffix(',00')} ₽'


async def _financial_summary(orders: list[Order]) -> str:
    paid_orders = [order for order in orders if order.payment_status == 'paid']
    unpaid_orders = [
        order for order in orders
        if order.payment_status not in {'paid', 'cancelled'}
    ]
    total = lambda field: sum(
        (getattr(order, field) for order in paid_orders), Decimal('0'),
    )

    owner_earnings = {
        'Диана': total('diana_share'),
        'Булат': total('bulat_share'),
    }
    promocodes = {
        promocode.id: promocode
        for promocode in await Promocode.get_all()
    }
    partner_debts = defaultdict(lambda: Decimal('0'))
    partner_orders = defaultdict(int)
    unknown_partner_debt = Decimal('0')
    for order in paid_orders:
        if not order.partner_reward:
            continue
        promocode = promocodes.get(order.promo_code_id)
        if not promocode:
            unknown_partner_debt += order.partner_reward
            continue
        partner_name = promocode.partner_name.strip() or f'Промокод {promocode.code}'
        partner_debts[partner_name] += order.partner_reward
        partner_orders[partner_name] += 1

    owner_lines = '\n'.join(
        f'• {html.escape(name)}: <b>{_rubles(amount)}</b>'
        for name, amount in owner_earnings.items()
    )
    partner_lines = '\n'.join(
        f'• {html.escape(name)}: <b>{_rubles(amount)}</b> '
        f'({partner_orders[name]} заказов)'
        for name, amount in sorted(partner_debts.items())
    )
    if unknown_partner_debt:
        partner_lines += (
            ('\n' if partner_lines else '')
            + f'• Удалённые промокоды: <b>{_rubles(unknown_partner_debt)}</b>'
        )
    partner_lines = partner_lines or '• Выплат пока нет'

    product_discounts = total('product_discount_total')
    order_discounts = sum(
        (
            order.subtotal
            - order.product_discount_total
            - order.paid_total
            - order.coins_used
            for order in paid_orders
        ),
        Decimal('0'),
    )
    paid_total = total('paid_total')
    owner_total = sum(owner_earnings.values(), Decimal('0'))
    partner_total = total('partner_reward')
    payouts = await PartnerPayout.get_recent(limit=None)
    partner_paid = sum(
        (payout.payout_amount for payout in payouts if payout.status == 'paid'),
        Decimal('0'),
    )
    partner_pending = sum(
        (payout.payout_amount for payout in payouts if payout.status == 'pending'),
        Decimal('0'),
    )
    partner_current = sum(
        (
            order.partner_reward for order in paid_orders
            if order.partner_payout_id is None
        ),
        Decimal('0'),
    )
    average_order = paid_total / len(paid_orders) if paid_orders else Decimal('0')
    awaiting_total = sum(
        (order.paid_total for order in unpaid_orders), Decimal('0'),
    )
    return (
        '<b>Финансы по подтверждённым оплатам</b>\n'
        f'Сформировано: {datetime.now():%d.%m.%Y %H:%M}\n'
        f'Оплаченных заказов: <b>{len(paid_orders)}</b>\n'
        f'Оборот: <b>{_rubles(paid_total)}</b>\n'
        f'Средний чек: <b>{_rubles(average_order)}</b>\n'
        f'Себестоимость: <b>{_rubles(total('wholesale_total'))}</b>\n'
        f'Скидки на товары: <b>{_rubles(product_discounts)}</b>\n'
        f'Персональные скидки/промокоды: <b>{_rubles(order_discounts)}</b>\n'
        f'Списано коинов: <b>{_rubles(total('coins_used'))}</b>\n'
        f'Прибыль товаров: <b>{_rubles(total('net_profit'))}</b>\n'
        f'Ожидается оплат: <b>{_rubles(awaiting_total)}</b> '
        f'({len(unpaid_orders)} заказов)\n\n'
        '<b>К выплате владельцам (за всю историю)</b>\n'
        f'{owner_lines}\n'
        f'Итого владельцам: <b>{_rubles(owner_total)}</b>\n\n'
        '<b>Начислено промопартнёрам за всю историю</b>\n'
        f'{partner_lines}\n'
        f'Всего начислено: <b>{_rubles(partner_total)}</b>\n'
        f'Текущий период: <b>{_rubles(partner_current)}</b>\n'
        f'Ожидает выплаты: <b>{_rubles(partner_pending)}</b>\n'
        f'Уже выплачено: <b>{_rubles(partner_paid)}</b>\n'
        f'Осталось выплатить: <b>{_rubles(partner_current + partner_pending)}</b>'
    )


async def _answer_with_navigation(message: Message, text: str, section: str = 'menu'):
    return await message.answer(text, reply_markup=_back_keyboard(section))


def _admin_navigation(section: str, page: int, total: int) -> list[tuple[str, Any]]:
    buttons = []
    if page > 0:
        buttons.append((
            f'← {page}/{total}',
            AdminSectionCallback(section=section, page=page - 1),
        ))

    if page + 1 < total:
        buttons.append((
            f'{page + 2}/{total} →',
            AdminSectionCallback(section=section, page=page + 1),
        ))

    return buttons + [('🏠 Меню', AdminSectionCallback(section='menu'))]


def _user_navigation(
    user_id: int, section: str, page: int, total: int,
) -> list[tuple[str, Any]]:
    buttons = []
    if page > 0:
        buttons.append((
            f'← {page}/{total}',
            UserSectionCallback(
                user_id=user_id, section=section, page=page - 1,
            ),
        ))

    if page + 1 < total:
        buttons.append((
            f'{page + 2}/{total} →',
            UserSectionCallback(
                user_id=user_id, section=section, page=page + 1,
            ),
        ))

    return buttons + [
        (
            '← Пользователь',
            UserSectionCallback(user_id=user_id, section='card'),
        ),
        ('🏠 Меню', AdminSectionCallback(section='menu')),
    ]


def _help_text() -> str:
    return (
        '<b>Команды администратора</b>\n\n'
        '/admin — панель с административными подразделами\n'
        '/user &lt;username/Telegram ID&gt; — карточка пользователя\n'
        '/discount &lt;username/ID&gt; &lt;0–100&gt; — персональная скидка\n'
        '/availability &lt;ID&gt; &lt;есть|нет|уточняется&gt; '
        '[количество] [комментарий]\n'
        '/order &lt;ID&gt; &lt;оплачен|отмена&gt; — изменить заказ\n'
        '/promo CODE | Партнёр | скидка | вознаграждение — создать или обновить\n'
        '/promo_toggle CODE — включить или выключить промокод\n'
        '/sync_products — синхронизировать Google Sheets «Fyvessa Admin»\n\n'
        'Также поддерживаются значения available, unavailable, on_request, paid и cancel.\n\n'
        'Безопасность: команды и кнопки работают только в настроенном админском чате.'
    )


@router.message(Command('admin'))
async def admin_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(ADMIN_TEXT, reply_markup=_admin_keyboard())


async def _sync_text() -> str:
    try:
        report = await sync_catalog()
        await sync_settings()
    except (CatalogValidationError, SettingsValidationError) as exc:
        return (
            '❌ <b>Google Sheets не синхронизирован</b>\n'
            f'<pre>{html.escape(str(exc))}</pre>'
        )

    return (
        '✅ <b>Каталог и настройки синхронизированы</b>\n\n'
        f'Создано товаров: {report.products_created}\n'
        f'Обновлено товаров: {report.products_updated}\n'
        f'Скрыто товаров: {report.products_hidden}\n'
        f'Создано категорий: {report.categories_created}\n'
        f'Исправлено строк: {report.rows_corrected}'
    )


@router.message(Command('sync_products'))
async def sync_products_command(message: Message):
    status_message = await message.answer(SYNC_STARTED_TEXT)
    await status_message.edit_text(
        await _sync_text(),
        reply_markup=_back_keyboard(),
    )


async def _user_text(user: User) -> str:
    orders = await Order.get_recent(user_id=user.id, limit=None)
    requests = await AvailabilityRequest.get_recent(user_id=user.id, limit=None)
    username = f'@{html.escape(user.username)}' if user.username else 'нет'
    name = html.escape(
        ' '.join(part for part in (user.first_name, user.last_name) if part)
        or 'не указано'
    )

    return (
        f'<b>Пользователь {user.id}</b>\n\n'
        f'Имя: {name}\n'
        f'Username: {username}\n'
        f'Telegram ID: <code>{user.id}</code>\n'
        f'Телефон: {html.escape(user.phone_number or 'не указан')}\n'
        f'Дата рождения: {user.birth_date or 'не указана'}\n'
        f'Регистрация: {'✅ заполнена' if user.is_registered else '⚠️ не завершена'}\n'
        f'Коины: {user.coin_balance}\n'
        f'Персональная скидка: {user.personal_discount_percent}%\n'
        f'Пригласил: {user.referrer_id or 'нет'}\n'
        f'Заказов: {len(orders)}\n'
        f'Запросов наличия: {len(requests)}\n'
        f'Создан: {user.created_at:%d.%m.%Y}'
    )


def _user_keyboard(user_id: int, page: int = 0, total: int = 1):
    buttons = [
        (text, UserSectionCallback(user_id=user_id, section=section))
        for text, section in (
            ('📦 Заказы', 'orders'),
            ('🔎 Запросы наличия', 'availability'),
            ('🪙 История коинов', 'coins'),
            ('🔄 Обновить карточку', 'card'),
        )
    ]
    buttons.append((
        '± Изменить коины',
        CoinSettingsCallback(action='adjust', user_id=user_id),
    ))

    if page > 0:
        buttons.append((
            f'← {page}/{total}',
            AdminSectionCallback(section='users', page=page - 1),
        ))

    if page + 1 < total:
        buttons.append((
            f'{page + 2}/{total} →',
            AdminSectionCallback(section='users', page=page + 1),
        ))

    buttons.append(('🏠 Меню', AdminSectionCallback(section='menu')))
    return inline_keyboard(buttons, 2, 2, 1, 2, 1)


@router.message(Command('user'))
@transaction(1)
async def user_command(message: Message, command: CommandObject):
    user = await User.find(command.args or '')
    if not user:
        return await _answer_with_navigation(
            message,
            'Пользователь не найден. Используйте <code>/user username</code> или '
            '<code>/user 123456789</code>.',
        )

    await message.answer(
        await _user_text(user),
        reply_markup=_user_keyboard(user.id),
    )


def _order_text(order: Order, user: Optional[User] = None) -> str:
    username = f' (@{html.escape(user.username)})' if user and user.username else ''
    return (
        f'<b>{html.escape(order.number)}</b> · {order.paid_total} ₽\n'
        f'ID заказа: <code>{order.id}</code>\n'
        f'{ORDER_STATUS_LABELS.get(order.status, order.status)} · '
        f'доставка: {SHIPPING_STATUS_LABELS.get(order.shipping_status, order.shipping_status)} · '
        f'{order.created_at:%d.%m.%Y %H:%M}\n'
        f'Пользователь: <code>{order.user_id}</code>{username}\n'
        f'Получатель: {html.escape(order.recipient_first_name)} '
        f'{html.escape(order.recipient_last_name)}, {html.escape(order.recipient_phone_number)}\n'
        f'{DELIVERY_METHOD_LABELS.get(order.delivery_method, order.delivery_method)}: '
        f'{html.escape(order.pickup_point_address)}'
    )


def _order_keyboard(order: Order, user_id: int = 0, page: int = 0, total: int = 1):
    buttons = []
    if order.status == 'payment_review':
        buttons.append((
            '✅ Подтвердить оплату',
            OrderActionCallback(
                order_id=order.id, action='paid',
            ),
        ))

    if order.payment_status != 'paid' and order.status != 'cancelled':
        buttons.append((
            '✖️ Отменить',
            OrderActionCallback(
                order_id=order.id, action='cancel',
            ),
        ))

    next_shipping_status = {
        'created': ('📦 Начать сборку', 'assembling'),
        'assembling': ('🚚 Отправлен', 'shipped'),
        'shipped': ('✅ Доставлен', 'delivered'),
    }.get(order.shipping_status)
    if order.payment_status == 'paid' and next_shipping_status:
        button_text, status = next_shipping_status
        buttons.append((
            button_text,
            OrderActionCallback(order_id=order.id, action=status),
        ))

    navigation = (
        _user_navigation(user_id, 'orders', page, total)
        if user_id
        else _admin_navigation('orders', page, total)
    )

    return inline_keyboard(buttons + navigation, 1, 2, 1)


async def _availability_text(availability: AvailabilityRequest) -> str:
    product = await Product.get_by_id(availability.product_id)
    user = await User.get_by_id(availability.user_id)
    if not user:
        return f'<b>Запрос №{availability.id}</b> · пользователь удалён'

    username = f'@{html.escape(user.username)}' if user.username else 'без username'
    details = (
        f'<b>Запрос №{availability.id}</b> · '
        f'{AVAILABILITY_LABELS.get(availability.status, availability.status)}\n'
        f'Товар: {html.escape(product.name if product else 'удалён')}\n'
        f'Количество: {availability.requested_quantity or 1}\n'
        f'Пользователь: <code>{user.id}</code> ({username})'
    )
    if availability.available_quantity is not None:
        details += f'\nПодтверждено: {availability.available_quantity} шт.'
    if availability.admin_comment:
        details += f'\nКомментарий: {html.escape(availability.admin_comment)}'

    return details


def _availability_admin_keyboard(availability: AvailabilityRequest, user_id: int = 0, page: int = 0, total: int = 1):
    buttons = (
        [
            (text, AvailabilityActionCallback(request_id=availability.id, status=status))
            for text, status in (
            ('✅ Есть', 'available'),
            ('❌ Нет', 'unavailable'),
            ('⏳ Уточняется', 'on_request'),
        )
        ]
        if availability.status == 'pending'
        else []
    )

    navigation = (
        _user_navigation(user_id, 'availability', page, total)
        if user_id
        else _admin_navigation('availability', page, total)
    )

    return inline_keyboard(buttons + navigation, 2, 1, 2, 1)


async def _promocode_text(promocode: Promocode) -> str:
    usage_count = await Order.count_for_promocode(promocode.id)
    return (
        f'🎟 <b>Промокод:</b> <code>{html.escape(promocode.code)}</code>\n'
        f'👤 <b>Владелец:</b> {html.escape(promocode.partner_name)}\n\n'
        f'Скидка {promocode.user_discount_percent}% · '
        f'вознаграждение {promocode.partner_reward_percent}% · '
        f'{'активен' if promocode.is_active else 'отключён'}\n'
        f'Использований: <b>{usage_count}</b>'
    )


def _promocode_keyboard(promocode: Promocode, page: int, total: int):
    return inline_keyboard([
        (
            'Выключить' if promocode.is_active else 'Включить',
            PromocodeToggleCallback(promocode_id=promocode.id),
        ),
        (
            '➕ Добавить',
            PromocodeActionCallback(
                action='create', promocode_id=promocode.id, page=page,
            ),
        ),
        (
            '🗑 Удалить',
            PromocodeActionCallback(
                action='delete', promocode_id=promocode.id, page=page,
            ),
        ),
        *_admin_navigation('promos', page, total),
    ], 1, 2, 2, 1)


def _promocode_form_keyboard(promocode_id: int = 0, page: int = 0):
    return inline_keyboard([
        (
            '← Отмена',
            PromocodeActionCallback(
                action='list', promocode_id=promocode_id, page=page,
            ),
        ),
        ('🏠 Меню', AdminSectionCallback(section='menu')),
    ])


def _empty_promocodes_keyboard():
    return inline_keyboard([
        ('➕ Добавить промокод', PromocodeActionCallback(action='create')),
        ('🏠 Меню', AdminSectionCallback(section='menu')),
    ])


async def _show_promocodes(callback: CallbackQuery, page: int = 0):
    promocodes = await Promocode.get_recent()
    if not promocodes:
        await _edit_message(
            callback,
            '<b>Промокоды</b>\n\nПромокодов пока нет.',
            _empty_promocodes_keyboard(),
        )
        return

    page = min(page, len(promocodes) - 1)
    promocode = promocodes[page]
    await _edit_message(
        callback, await _promocode_text(promocode),
        _promocode_keyboard(promocode, page, len(promocodes)),
    )


def _parse_promocode_details(value: str) -> tuple[str, str, Decimal, Decimal]:
    parts = [part.strip() for part in value.split('|')]
    if len(parts) != 4:
        raise ValueError(
            'Формат: CODE | Имя партнёра | Процент скидки | '
            'Процент вознаграждения'
        )

    try:
        user_percent = Decimal(parts[2].replace(',', '.'))
        partner_percent = Decimal(parts[3].replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError('Проценты должны быть числами') from exc

    if not all((parts[0], parts[1])):
        raise ValueError('Код и имя партнёра не могут быть пустыми')
    if not all(
        Decimal('0') <= percent <= Decimal('100')
        for percent in (user_percent, partner_percent)
    ):
        raise ValueError('Проценты должны быть в диапазоне 0–100')

    return parts[0].upper(), parts[1], user_percent, partner_percent


async def _save_promocode(value: str) -> Promocode:
    code, partner_name, user_percent, partner_percent = _parse_promocode_details(value)
    promocode = await Promocode.get_by_code(code)
    if not promocode:
        promocode = Promocode(code=code, partner_name=partner_name)

    promocode.partner_name = partner_name
    promocode.user_discount_percent = user_percent
    promocode.partner_reward_percent = partner_percent
    promocode.is_active = True
    promocode.add()
    await session_context.get().flush()
    return promocode


def _section_navigation(
    section: str,
    page: int,
    total: int,
    parent: str,
) -> list[tuple[str, Any]]:
    buttons = []
    if page > 0:
        buttons.append((
            f'← {page}/{total}',
            AdminSectionCallback(section=section, page=page - 1),
        ))
    if page + 1 < total:
        buttons.append((
            f'{page + 2}/{total} →',
            AdminSectionCallback(section=section, page=page + 1),
        ))
    buttons.extend([
        ('← К разделу', AdminSectionCallback(section=parent)),
        ('🏠 Меню', AdminSectionCallback(section='menu')),
    ])
    return buttons


async def _show_payout_menu(callback: CallbackQuery) -> None:
    pending = await PartnerPayout.get_recent(status='pending', limit=None)
    archive = await PartnerPayout.get_recent(status='paid', limit=None)
    await _edit_message(
        callback,
        '<b>Выплаты партнёрам</b>\n\n'
        f'Следующая фиксация: <b>{next_payout_cutoff():%d.%m.%Y %H:%M}</b>\n'
        f'Ожидают выплаты: <b>{len(pending)}</b>\n'
        f'В архиве: <b>{len(archive)}</b>',
        inline_keyboard([
            ('📈 Текущие начисления', AdminSectionCallback(section='payout_current')),
            ('⏳ Ожидают выплаты', AdminSectionCallback(section='payout_pending')),
            ('🗃 Архив выплат', AdminSectionCallback(section='payout_archive')),
            ('🏠 Меню', AdminSectionCallback(section='menu')),
        ]),
    )


async def _show_current_accruals(callback: CallbackQuery, page: int = 0) -> None:
    accruals = await current_partner_accruals()
    if not accruals:
        return await _edit_message(
            callback,
            '<b>Текущие начисления</b>\n\nПартнёрских промокодов пока нет.',
            _back_keyboard(),
        )
    page = min(page, len(accruals) - 1)
    accrual = accruals[page]
    promocode = accrual.promocode
    await _edit_message(
        callback,
        '<b>Текущие начисления</b>\n\n'
        f'Партнёр: <b>{html.escape(promocode.partner_name)}</b>\n'
        f'Промокод: <code>{html.escape(promocode.code)}</code>\n'
        f'Оплаченных заказов: <b>{accrual.orders_count}</b>\n'
        f'Сумма заказов: <b>{_rubles(accrual.orders_total)}</b>\n'
        f'Процент по начислениям: <b>{accrual.reward_percent}%</b>\n'
        f'К выплате: <b>{_rubles(accrual.payout_amount)}</b>\n'
        f'Следующая фиксация: <b>{next_payout_cutoff():%d.%m.%Y %H:%M}</b>',
        inline_keyboard(
            _section_navigation('payout_current', page, len(accruals), 'payouts'),
            2, 1, 1,
        ),
    )


def _payout_text(payout: PartnerPayout) -> str:
    paid_line = (
        f'\nФактически выплачено: <b>{payout.paid_at:%d.%m.%Y %H:%M}</b>'
        if payout.paid_at else ''
    )
    return (
        f'<b>{"Архивная выплата" if payout.status == "paid" else "Ожидает выплаты"}</b>\n\n'
        f'Партнёр: <b>{html.escape(payout.partner_name_snapshot)}</b>\n'
        f'Промокод: <code>{html.escape(payout.promo_code_snapshot)}</code>\n'
        f'Сформировано: {payout.generated_at:%d.%m.%Y %H:%M}\n'
        f'Заказов: <b>{payout.orders_count}</b>\n'
        f'Сумма заказов: <b>{_rubles(payout.orders_total)}</b>\n'
        f'Процент: <b>{payout.reward_percent_snapshot}%</b>\n'
        f'Сумма выплаты: <b>{_rubles(payout.payout_amount)}</b>\n'
        f'Статус: <b>{"Выплачено" if payout.status == "paid" else "Ожидает выплаты"}</b>'
        f'{paid_line}'
    )


async def _show_payouts(
    callback: CallbackQuery,
    status: str,
    section: str,
    page: int = 0,
) -> None:
    payouts = await PartnerPayout.get_recent(status=status, limit=None)
    if not payouts:
        return await _edit_message(
            callback,
            '<b>Выплаты партнёрам</b>\n\nЗаписей в этом разделе пока нет.',
            inline_keyboard([
                ('← К выплатам', AdminSectionCallback(section='payouts')),
                ('🏠 Меню', AdminSectionCallback(section='menu')),
            ]),
        )
    page = min(page, len(payouts) - 1)
    payout = payouts[page]
    buttons = []
    if status == 'pending':
        buttons.append((
            '✅ Оплачено',
            PayoutActionCallback(action='paid', payout_id=payout.id, page=page),
        ))
    buttons.extend(_section_navigation(section, page, len(payouts), 'payouts'))
    await _edit_message(callback, _payout_text(payout), inline_keyboard(buttons, 1, 2, 1, 1))


def _valid_url(value: str, allow_local: bool = False) -> str:
    value = value.strip()
    if allow_local and value.startswith('/'):
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError('Ссылка должна начинаться с https://')
    return value


def _banner_text(banner: Banner) -> str:
    image = html.escape(banner.image_url) if banner.image_url else 'без изображения'
    return (
        f'<b>Рекламная карточка №{banner.id}</b>\n\n'
        f'Название: <b>{html.escape(banner.title)}</b>\n'
        f'Изображение: {image}\n'
        f'Переход: {html.escape(banner.target_url)}\n'
        f'Порядок: <b>{banner.position}</b>\n'
        f'Статус: <b>{"показывается" if banner.is_active else "скрыта"}</b>'
    )


def _banner_keyboard(banner: Banner, page: int, total: int):
    return inline_keyboard([
        ('✏️ Изменить', BannerActionCallback(action='edit', banner_id=banner.id, page=page)),
        (
            '🙈 Скрыть' if banner.is_active else '👁 Показать',
            BannerActionCallback(action='toggle', banner_id=banner.id, page=page),
        ),
        ('➕ Добавить', BannerActionCallback(action='add', banner_id=banner.id, page=page)),
        ('🗑 Удалить', BannerActionCallback(action='delete', banner_id=banner.id, page=page)),
        *_admin_navigation('banners', page, total),
    ], 2, 2, 2, 1)


async def _show_banners(callback: CallbackQuery, page: int = 0) -> None:
    banners = await Banner.get_all()
    if not banners:
        return await _edit_message(
            callback,
            '<b>Рекламные карточки</b>\n\nКарточек пока нет.',
            inline_keyboard([
                ('➕ Добавить', BannerActionCallback(action='add')),
                ('🏠 Меню', AdminSectionCallback(section='menu')),
            ]),
        )
    page = min(page, len(banners) - 1)
    await _edit_message(
        callback,
        _banner_text(banners[page]),
        _banner_keyboard(banners[page], page, len(banners)),
    )


def _social_text(channel: SocialChannel) -> str:
    check_mode = 'автоматически' if channel.supports_automatic_check else 'администратором'
    return (
        f'<b>Социальная сеть №{channel.id}</b>\n\n'
        f'Площадка: <b>{html.escape(channel.platform)}</b>\n'
        f'Аккаунт: <b>{html.escape(channel.account_name)}</b>\n'
        f'Ссылка: {html.escape(channel.url)}\n'
        f'Награда пригласившему: <b>{channel.coin_reward} коинов</b>\n'
        f'Награда подписавшемуся: <b>{channel.invitee_coin_reward} коинов</b>\n'
        f'Проверка: <b>{check_mode}</b>\n'
        f'Telegram chat ID: <code>{html.escape(channel.telegram_chat_id or "—")}</code>\n'
        f'Статус: <b>{"включена" if channel.is_active else "отключена"}</b>'
    )


def _social_keyboard(channel: SocialChannel, page: int, total: int):
    return inline_keyboard([
        ('✏️ Изменить', SocialActionCallback(action='edit', channel_id=channel.id, page=page)),
        (
            '⏸ Отключить' if channel.is_active else '▶️ Включить',
            SocialActionCallback(action='toggle', channel_id=channel.id, page=page),
        ),
        ('➕ Добавить', SocialActionCallback(action='add', channel_id=channel.id, page=page)),
        *_admin_navigation('socials', page, total),
    ], 2, 1, 2, 1)


async def _show_socials(callback: CallbackQuery, page: int = 0) -> None:
    channels = await SocialChannel.get_all()
    if not channels:
        return await _edit_message(
            callback,
            '<b>Социальные сети</b>\n\nПлощадки пока не добавлены.',
            inline_keyboard([
                ('➕ Добавить', SocialActionCallback(action='add')),
                ('🏠 Меню', AdminSectionCallback(section='menu')),
            ]),
        )
    page = min(page, len(channels) - 1)
    await _edit_message(
        callback,
        _social_text(channels[page]),
        _social_keyboard(channels[page], page, len(channels)),
    )


async def _referral_review_text(reward: ReferralReward) -> str:
    invited = await User.get_by_id(reward.invited_user_id)
    referrer = await User.get_by_id(reward.referrer_id)
    channel = await SocialChannel.get_by_id(reward.social_channel_id)
    return (
        '<b>Реферальная проверка</b>\n\n'
        f'Подписывается: <code>{reward.invited_user_id}</code> '
        f'({html.escape(invited.username or "без username") if invited else "удалён"})\n'
        f'Пригласил: <code>{reward.referrer_id}</code> '
        f'({html.escape(referrer.username or "без username") if referrer else "удалён"})\n'
        f'Площадка: <b>{html.escape(channel.platform) if channel else "удалена"}</b> / '
        f'{html.escape(channel.account_name) if channel else "—"}\n'
        f'Награда пригласившему: <b>{reward.reward_amount} коинов</b>\n'
        f'Награда подписавшемуся: <b>{reward.invitee_reward_amount} коинов</b>\n'
        f'Статус: <b>{reward.status}</b>'
    )


async def _show_referral_reviews(callback: CallbackQuery, page: int = 0) -> None:
    rewards = await ReferralReward.get_recent(status='review')
    if not rewards:
        all_rewards = await ReferralReward.get_all()
        approved = sum(item.status == 'approved' for item in all_rewards)
        return await _edit_message(
            callback,
            '<b>Реферальные проверки</b>\n\n'
            'Ожидающих ручной проверки нет.\n'
            f'Всего подтверждено: <b>{approved}</b>',
            _back_keyboard(),
        )
    page = min(page, len(rewards) - 1)
    reward = rewards[page]
    await _edit_message(
        callback,
        await _referral_review_text(reward),
        inline_keyboard([
            ('✅ Подтвердить', ReferralReviewCallback(action='approve', reward_id=reward.id, page=page)),
            ('↩️ Вернуть', ReferralReviewCallback(action='reject', reward_id=reward.id, page=page)),
            *_admin_navigation('referrals', page, len(rewards)),
        ], 2, 2, 1),
    )


async def _show_coin_settings(callback: CallbackQuery) -> None:
    activation_reward = await get_referral_activation_reward()
    percent = await get_purchase_coin_percent()
    transactions = await CoinTransaction.get_recent(limit=5)
    history = '\n'.join(
        f'• {transaction.created_at:%d.%m %H:%M} · '
        f'<code>{transaction.user_id}</code> · {transaction.amount:+} · '
        f'{html.escape(transaction.reason)}'
        for transaction in transactions
    ) or '• Операций пока нет'
    await _edit_message(
        callback,
        '<b>Настройки коинов</b>\n\n'
        f'Награда за первую реферальную активацию: <b>{activation_reward} коинов</b>\n'
        f'Процент коинов с покупки: <b>{percent}%</b>\n\n'
        '<b>Последние начисления и списания</b>\n'
        f'{history}',
        inline_keyboard([
            ('✏️ Награда за активацию', CoinSettingsCallback(action='activation')),
            ('✏️ Изменить процент', CoinSettingsCallback(action='percent')),
            ('± Ручная операция', CoinSettingsCallback(action='adjust')),
            ('📜 Вся история', CoinSettingsCallback(action='history')),
            ('🌐 Социальные сети и награды', AdminSectionCallback(section='socials')),
            ('🏠 Меню', AdminSectionCallback(section='menu')),
        ]),
    )


def _coin_history_keyboard(page: int, total_pages: int, user_id: int = 0):
    buttons = []
    if page > 0:
        callback = (
            UserSectionCallback(user_id=user_id, section='coins', page=page - 1)
            if user_id
            else CoinSettingsCallback(action='history', page=page - 1)
        )
        buttons.append((f'← {page}/{total_pages}', callback))
    if page + 1 < total_pages:
        callback = (
            UserSectionCallback(user_id=user_id, section='coins', page=page + 1)
            if user_id
            else CoinSettingsCallback(action='history', page=page + 1)
        )
        buttons.append((f'{page + 2}/{total_pages} →', callback))
    if user_id:
        buttons.append((
            '← Пользователь',
            UserSectionCallback(user_id=user_id, section='card'),
        ))
    else:
        buttons.append(('← Настройки коинов', AdminSectionCallback(section='coins')))
    buttons.append(('🏠 Меню', AdminSectionCallback(section='menu')))
    return inline_keyboard(buttons, 2, 1, 1)


async def _show_coin_history(
    callback: CallbackQuery,
    page: int = 0,
    user_id: int = 0,
) -> None:
    total = await CoinTransaction.count(user_id=user_id or None)
    total_pages = max(1, (total + COIN_HISTORY_PAGE_SIZE - 1) // COIN_HISTORY_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    transactions = await CoinTransaction.get_page(
        page=page,
        page_size=COIN_HISTORY_PAGE_SIZE,
        user_id=user_id or None,
    )

    entries = []
    for transaction in transactions:
        order = (
            await Order.get_by_id(transaction.order_id)
            if transaction.order_id else None
        )
        order_text = (
            f' · заказ <b>{html.escape(order.number)}</b>'
            if order else ''
        )
        admin_text = (
            f' · админ <code>{transaction.admin_id}</code>'
            if transaction.admin_id else ''
        )
        entries.append(
            f'<b>#{transaction.id}</b> · {transaction.created_at:%d.%m.%Y %H:%M}\n'
            f'Пользователь <code>{transaction.user_id}</code>{admin_text}{order_text}\n'
            f'{transaction.amount:+} → баланс {transaction.balance_after}\n'
            f'{html.escape(transaction.reason)}'
        )

    scope = f' пользователя <code>{user_id}</code>' if user_id else ''
    history = '\n\n'.join(entries) or 'Операций пока нет.'
    await _edit_message(
        callback,
        f'<b>История коинов{scope}</b>\n'
        f'Страница {page + 1} из {total_pages} · всего операций: {total}\n\n'
        f'{history}',
        _coin_history_keyboard(page, total_pages, user_id),
    )


async def _resolve_availability(
    availability: AvailabilityRequest,
    status: str,
    available_quantity: Optional[int],
    comment: Optional[str],
    admin_id: int,
) -> bool:
    if availability.status != 'pending':
        return False

    if status not in {'available', 'unavailable', 'on_request'}:
        raise ValueError('Статус: есть, нет или уточняется')

    if status == 'available':
        if available_quantity is not None and available_quantity < 1:
            raise ValueError('Доступное количество должно быть больше нуля')

        available_quantity = (
            available_quantity or availability.requested_quantity or 1
        )
    elif status == 'unavailable':
        available_quantity = 0
    else:
        available_quantity = None

    availability.status = status
    availability.available_quantity = available_quantity
    availability.admin_comment = comment
    availability.admin_id = admin_id
    availability.resolved_at = datetime.now()
    availability.add()

    user = await User.get_by_id(availability.user_id)
    product = await Product.get_by_id(availability.product_id)
    if user:
        suffix_text = (
            f' Доступно: {available_quantity} шт.'
            if status == 'available' and available_quantity is not None
            else ''
        )

        note_text = f'\nКомментарий: {html.escape(comment)}' if comment else ''
        await bot.send_message(
            user.id,
            f'<b>Ответ по наличию</b>\n\n'
            f'{html.escape(product.name if product else 'Товар')}: '
            f'{AVAILABILITY_LABELS[status]}.{suffix_text}'
            f'{" Подтверждение действует 1 час." if status == "available" else ""}'
            f'{note_text}',
            reply_markup=_cart_keyboard() if status == 'available' else None,
        )

    return True


@router.callback_query(AdminSectionCallback.filter())
@transaction(1)
async def admin_section(callback: CallbackQuery, callback_data: AdminSectionCallback, state: FSMContext):
    await state.clear()
    if callback_data.section == 'menu':
        await _edit_message(callback, ADMIN_TEXT, _admin_keyboard())
    elif callback_data.section in ADMIN_GROUPS:
        await _edit_message(
            callback,
            _admin_group_text(callback_data.section),
            _admin_group_keyboard(callback_data.section),
        )
    elif callback_data.section == 'sync':
        await callback.answer('Синхронизация запущена')
        await _edit_message(callback, await _sync_text(), _back_keyboard())
    elif callback_data.section == 'help':
        await _edit_message(callback, _help_text(), _back_keyboard())
    elif callback_data.section == 'availability':
        requests = await AvailabilityRequest.get_recent(pending_only=True)
        if not requests:
            return await callback.answer('Запросов наличия нет.', show_alert=True)

        page = min(callback_data.page, len(requests) - 1)
        availability = requests[page]
        await _edit_message(
            callback, await _availability_text(availability),
            _availability_admin_keyboard(
                availability, page=page, total=len(requests),
            ),
        )

    elif callback_data.section == 'orders':
        orders = await Order.get_recent()
        if not orders:
            return await callback.answer('Заказов пока нет.', show_alert=True)

        page = min(callback_data.page, len(orders) - 1)
        order = orders[page]
        await _edit_message(
            callback, _order_text(order, await User.get_by_id(order.user_id)),
            _order_keyboard(order, page=page, total=len(orders)),
        )

    elif callback_data.section == 'users':
        users = await User.get_recent()
        if not users:
            return await callback.answer('Пользователей пока нет.', show_alert=True)

        page = min(callback_data.page, len(users) - 1)
        user = users[page]
        await _edit_message(
            callback, await _user_text(user),
            _user_keyboard(user.id, page, len(users)),
        )

    elif callback_data.section == 'promos':
        await _show_promocodes(callback, callback_data.page)

    elif callback_data.section == 'payouts':
        await _show_payout_menu(callback)

    elif callback_data.section == 'payout_current':
        await _show_current_accruals(callback, callback_data.page)

    elif callback_data.section == 'payout_pending':
        await _show_payouts(
            callback, 'pending', 'payout_pending', callback_data.page,
        )

    elif callback_data.section == 'payout_archive':
        await _show_payouts(
            callback, 'paid', 'payout_archive', callback_data.page,
        )

    elif callback_data.section == 'banners':
        await _show_banners(callback, callback_data.page)

    elif callback_data.section == 'socials':
        await _show_socials(callback, callback_data.page)

    elif callback_data.section == 'referrals':
        await _show_referral_reviews(callback, callback_data.page)

    elif callback_data.section == 'coins':
        await _show_coin_settings(callback)

    elif callback_data.section == 'summary':
        products = await Product.get_all()
        users = await User.get_all()
        orders = await Order.get_all()
        pending = await AvailabilityRequest.get_recent(
            pending_only=True, limit=None,
        )

        review = [order for order in orders if order.status == 'payment_review']
        financial_summary = await _financial_summary(orders)

        await _edit_message(
            callback,
            '<b>Текущая сводка</b>\n\n'
            f'Активных товаров: {sum(product.is_active for product in products)}\n'
            f'Пользователей: {len(users)}\n'
            f'Заказов: {len(orders)}\n'
            f'Оплат на проверке: {len(review)}\n'
            f'Запросов наличия без ответа: {len(pending)}\n'
            f'\n{financial_summary}',
            _back_keyboard(),
        )


@router.callback_query(UserSectionCallback.filter())
@transaction(1)
async def user_section(callback: CallbackQuery, callback_data: UserSectionCallback):
    user = await User.get_by_id(callback_data.user_id)
    if not user:
        return await callback.answer('Пользователь не найден', show_alert=True)

    if callback_data.section == 'orders':
        orders = await Order.get_recent(user_id=user.id, limit=10)
        if not orders:
            return await callback.answer('У пользователя пока нет заказов.', show_alert=True)

        page = min(callback_data.page, len(orders) - 1)
        order = orders[page]
        await _edit_message(
            callback, _order_text(order, user),
            _order_keyboard(
                order, user_id=user.id, page=page, total=len(orders),
            ),
        )

    elif callback_data.section == 'availability':
        requests = await AvailabilityRequest.get_recent(user_id=user.id)
        if not requests:
            return await callback.answer('Запросов наличия нет.', show_alert=True)

        page = min(callback_data.page, len(requests) - 1)
        availability = requests[page]
        await _edit_message(
            callback, await _availability_text(availability),
            _availability_admin_keyboard(
                availability, user_id=user.id, page=page, total=len(requests),
            ),
        )
    elif callback_data.section == 'coins':
        await _show_coin_history(callback, callback_data.page, user.id)
    else:
        await _edit_message(callback, await _user_text(user), _user_keyboard(user.id))


@router.callback_query(AvailabilityActionCallback.filter())
@transaction(1)
async def availability_action(callback: CallbackQuery, callback_data: AvailabilityActionCallback):
    availability = await AvailabilityRequest.get_by_id(callback_data.request_id)
    if not availability:
        return await callback.answer('Запрос не найден', show_alert=True)

    has_changed = await _resolve_availability(
        availability,
        callback_data.status,
        None,
        None,
        callback.from_user.id
    )

    if not has_changed:
        return await callback.answer('Запрос уже обработан', show_alert=True)

    await _edit_message(
        callback,
        await _availability_text(availability),
        _back_keyboard('availability'),
    )

    await callback.answer('Ответ отправлен')


@router.message(Command('availability'))
@transaction(1)
async def availability_command(message: Message, command: CommandObject):
    parts = (command.args or '').split(maxsplit=3)
    if len(parts) < 2 or not parts[0].isdigit():
        return await _answer_with_navigation(
            message,
            'Формат: <code>/availability ID есть 3 комментарий</code>',
            'availability',
        )

    quantity = None
    comment = None

    if len(parts) >= 3:
        try:
            quantity = int(parts[2])
            comment = parts[3] if len(parts) == 4 else None
        except ValueError:
            comment = ' '.join(parts[2:])

    availability = await AvailabilityRequest.get_by_id(int(parts[0]))
    if not availability:
        return await _answer_with_navigation(
            message, 'Запрос не найден.', 'availability',
        )

    try:
        has_changed = await _resolve_availability(
            availability,
            AVAILABILITY_COMMAND_STATUSES.get(parts[1].casefold(), parts[1].casefold()),
            quantity,
            comment,
            message.from_user.id
        )
    except ValueError as exc:
        return await _answer_with_navigation(message, html.escape(str(exc)), 'availability')

    await _answer_with_navigation(
        message,
        'Ответ отправлен.' if has_changed else 'Запрос уже обработан.',
        'availability',
    )


async def _apply_order_action(order: Order, action: str, admin_id: int) -> bool:
    if action == 'paid':
        has_changed = await confirm_payment(order, admin_id)
        coin_text = (
            f' Начислено <b>{order.purchase_coins_awarded}</b> коинов.'
            if order.purchase_coins_awarded else ''
        )
        message_text = (
            f'✅ Оплата заказа <b>{html.escape(order.number)}</b> подтверждена.'
            f'{coin_text}'
        )
    elif action == 'cancel':
        has_changed = await cancel_order(order, admin_id)
        message_text = f'Заказ <b>{html.escape(order.number)}</b> отменён.'
    elif action in {'assembling', 'shipped', 'delivered'}:
        has_changed = await update_shipping_status(order, action)
        label = SHIPPING_STATUS_LABELS[action]
        message_text = (
            f'📦 Статус доставки заказа <b>{html.escape(order.number)}</b>: '
            f'<b>{label}</b>.'
        )
    else:
        raise ValueError('Действие: оплачен, отмена, собирается, отправлен или доставлен')

    if has_changed:
        user = await User.get_by_id(order.user_id)
        if user:
            await bot.send_message(user.id, message_text)

    return has_changed


@router.callback_query(OrderActionCallback.filter())
@transaction(1)
async def order_action(callback: CallbackQuery, callback_data: OrderActionCallback):
    order = await Order.get_by_id(callback_data.order_id)
    if not order:
        return await callback.answer('Заказ не найден', show_alert=True)

    try:
        has_changed = await _apply_order_action(
            order,
            callback_data.action,
            callback.from_user.id,
        )
    except ValueError as exc:
        return await callback.answer(str(exc), show_alert=True)

    if not has_changed:
        return await callback.answer('Уже выполнено', show_alert=True)

    user = await User.get_by_id(order.user_id)
    orders = await Order.get_recent()
    page = next(
        (index for index, item in enumerate(orders) if item.id == order.id), 0,
    )

    await _edit_message(
        callback, _order_text(order, user),
        _order_keyboard(order, page=page, total=max(len(orders), 1)),
    )

    await callback.answer('Готово')


@router.message(Command('order'))
@transaction(1)
async def order_command(message: Message, command: CommandObject):
    parts = (command.args or '').split()
    if len(parts) != 2 or not parts[0].isdigit():
        return await _answer_with_navigation(
            message,
            'Формат: <code>/order ID оплачен|отмена|собирается|отправлен|доставлен</code>',
            'orders',
        )

    order = await Order.get_by_id(int(parts[0]))
    if not order:
        return await _answer_with_navigation(message, 'Заказ не найден.', 'orders')

    try:
        action = ORDER_COMMAND_ACTIONS.get(parts[1].casefold(), parts[1].casefold())
        has_changed = await _apply_order_action(order, action, message.from_user.id)
    except ValueError as exc:
        return await _answer_with_navigation(
            message, html.escape(str(exc)), 'orders',
        )

    await _answer_with_navigation(
        message,
        'Готово.' if has_changed else 'Это действие уже выполнено.',
        'orders',
    )


@router.message(Command('discount'))
@transaction(1)
async def discount_command(message: Message, command: CommandObject):
    parts = (command.args or '').rsplit(maxsplit=1)
    if len(parts) != 2:
        return await _answer_with_navigation(
            message, 'Формат: <code>/discount username 5</code>',
        )

    user = await User.find(parts[0])
    try:
        percent = Decimal(parts[1].replace(',', '.'))
    except InvalidOperation:
        percent = Decimal('-1')
    if not user or not Decimal('0') <= percent <= Decimal('100'):
        await _answer_with_navigation(
            message,
            'Пользователь не найден или процент вне диапазона 0–100.',
        )
        return

    user.personal_discount_percent = percent
    user.updated_at = datetime.now()
    user.add()

    await message.answer(
        f'Персональная скидка пользователя {user.id}: {percent}%.',
        reply_markup=_user_keyboard(user.id),
    )


@router.message(Command('promo'))
@transaction(1)
async def promocode_command(
    message: Message, command: CommandObject, state: FSMContext,
):
    await state.clear()
    try:
        promocode = await _save_promocode(command.args or '')
    except ValueError as exc:
        return await _answer_with_navigation(
            message, html.escape(str(exc)), 'promos',
        )

    await _answer_with_navigation(
        message,
        f'Промокод <b>{html.escape(promocode.code)}</b> сохранён и активен.',
        'promos',
    )


@router.message(Command('promo_toggle'))
@transaction(1)
async def promocode_toggle_command(message: Message, command: CommandObject):
    code = (command.args or '').strip().upper()
    promocode = await Promocode.get_by_code(code)
    if not promocode:
        return await _answer_with_navigation(
            message, 'Промокод не найден.', 'promos',
        )

    promocode.toggle()
    await _answer_with_navigation(
        message,
        f'{promocode.code}: {'включён' if promocode.is_active else 'отключён'}.',
        'promos',
    )


async def _edit_promocode_prompt(
    message: Message, message_id: int, text: str, reply_markup,
):
    try:
        await bot.get_bot().edit_message_text(
            chat_id=message.chat.id, message_id=message_id,
            text=text, reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if 'message is not modified' not in str(exc):
            await message.answer(text, reply_markup=reply_markup)


@router.message(PromocodeForm.details)
@transaction(1)
async def promocode_form(message: Message, state: FSMContext):
    data = await state.get_data()
    message_id = data.get('message_id')
    promocode_id = data.get('promocode_id', 0)
    page = data.get('page', 0)
    try:
        promocode = await _save_promocode(message.text or '')
    except ValueError as exc:
        error_text = (
            f'❌ {html.escape(str(exc))}.\n\n'
            'Отправьте данные ещё раз одним сообщением:\n'
            '<code>CODE | Имя партнёра | 10 | 10</code>'
        )

        if message_id:
            return await _edit_promocode_prompt(
                message, message_id, error_text,
                _promocode_form_keyboard(promocode_id, page),
            )

        return await message.answer(
            error_text,
            reply_markup=_promocode_form_keyboard(promocode_id, page),
        )

    await state.clear()
    promocodes = await Promocode.get_recent()
    page = next(
        (index for index, item in enumerate(promocodes) if item.id == promocode.id),
        0,
    )

    message_text = await _promocode_text(promocode)
    reply_markup = _promocode_keyboard(promocode, page, len(promocodes))
    if message_id:
        return await _edit_promocode_prompt(message, message_id, message_text, reply_markup)

    await message.answer(message_text, reply_markup=reply_markup)


@router.callback_query(PromocodeActionCallback.filter())
@transaction(1)
async def promocode_action(
    callback: CallbackQuery, callback_data: PromocodeActionCallback,
    state: FSMContext,
):
    if callback_data.action == 'create':
        await state.set_state(PromocodeForm.details)
        await state.update_data(
            message_id=callback.message.message_id,
            promocode_id=callback_data.promocode_id,
            page=callback_data.page,
        )
        return await _edit_message(
            callback,
            '<b>Новый промокод</b>\n\n'
            'Отправьте одним сообщением:\n'
            '<code>CODE | Имя партнёра | 10 | 10</code>\n\n'
            'Первое число — скидка покупателя, второе — вознаграждение партнёра.',
            _promocode_form_keyboard(
                callback_data.promocode_id, callback_data.page,
            ),
        )

    await state.clear()
    if callback_data.action == 'list':
        return await _show_promocodes(callback, callback_data.page)

    promocode = await Promocode.get_by_id(callback_data.promocode_id)
    if not promocode or promocode.is_deleted:
        return await callback.answer('Промокод не найден', show_alert=True)

    if callback_data.action == 'delete':
        usage_count = await Order.count_for_promocode(promocode.id)
        return await _edit_message(
            callback,
            f'<b>Удалить промокод {html.escape(promocode.code)}?</b>\n\n'
            f'Использований: {usage_count}. История прежних заказов сохранится.',
            inline_keyboard([
                (
                    '🗑 Да, удалить',
                    PromocodeActionCallback(
                        action='confirm_delete',
                        promocode_id=promocode.id,
                        page=callback_data.page,
                    ),
                ),
                (
                    '← Отмена',
                    PromocodeActionCallback(
                        action='list',
                        promocode_id=promocode.id,
                        page=callback_data.page,
                    ),
                ),
                ('🏠 Меню', AdminSectionCallback(section='menu')),
            ]),
        )

    if callback_data.action == 'confirm_delete':
        promocode.mark_deleted()
        await _show_promocodes(callback, callback_data.page)
        return await callback.answer('Промокод удалён')

    return await callback.answer('Действие не найдено', show_alert=True)


@router.callback_query(PromocodeToggleCallback.filter())
@transaction(1)
async def promocode_toggle_callback(
    callback: CallbackQuery, callback_data: PromocodeToggleCallback,
):
    promocode = await Promocode.get_by_id(callback_data.promocode_id)
    if not promocode or promocode.is_deleted:
        return await callback.answer('Промокод не найден', show_alert=True)

    promocode.toggle()
    promocodes = await Promocode.get_recent()
    page = next(
        (
            index for index, item in enumerate(promocodes)
            if item.id == promocode.id
        ),
        0,
    )

    await _edit_message(
        callback, await _promocode_text(promocode),
        _promocode_keyboard(promocode, page, max(len(promocodes), 1)),
    )

    await callback.answer('Включён' if promocode.is_active else 'Отключён')


@router.callback_query(PayoutActionCallback.filter())
@transaction(1)
async def payout_action(
    callback: CallbackQuery,
    callback_data: PayoutActionCallback,
):
    payout = await PartnerPayout.get_by_id(callback_data.payout_id)
    if not payout:
        return await callback.answer('Выплата не найдена', show_alert=True)
    if callback_data.action == 'paid':
        changed = await mark_payout_paid(payout, callback.from_user.id)
        await _show_payouts(
            callback,
            'pending',
            'payout_pending',
            callback_data.page,
        )
        return await callback.answer('Перенесено в архив' if changed else 'Уже выплачено')
    await callback.answer('Действие не найдено', show_alert=True)


def _banner_prompt(banner: Banner | None = None) -> str:
    current = (
        '\n\nДля сохранения текущего изображения укажите <code>-</code>.'
        if banner else ''
    )
    return (
        f'<b>{"Изменить" if banner else "Новая"} рекламная карточка</b>\n\n'
        'Вариант с URL — отправьте:\n'
        '<code>Название | https://image.jpg | /catalog | 10</code>\n\n'
        'Вариант с фото или файлом-изображением — прикрепите его и добавьте подпись:\n'
        '<code>Название | /catalog | 10</code>\n\n'
        'Поддерживаются JPEG, PNG, WebP, GIF и AVIF.\n\n'
        'Последнее число задаёт порядок. Ссылка перехода может быть внутренней '
        '(/catalog) или внешней (https://...).'
        f'{current}'
    )


def _banner_upload(message: Message) -> tuple[str, str] | None:
    if message.photo:
        return message.photo[-1].file_id, '.jpg'
    if not message.document:
        return None

    suffix = BANNER_IMAGE_SUFFIXES.get((message.document.mime_type or '').casefold())
    if not suffix:
        raise ValueError('Файл должен быть изображением JPEG, PNG, WebP, GIF или AVIF')
    return message.document.file_id, suffix


def _banner_parts(value: str | None, count: int, hint: str) -> list[str]:
    parts = [part.strip() for part in (value or '').split('|')]
    if len(parts) != count:
        raise ValueError(hint)
    return parts


def _parse_banner_details(
    message: Message,
    banner: Banner | None,
) -> tuple[str, str, str, int, tuple[str, str] | None]:
    upload = _banner_upload(message)
    if upload:
        title, target_url, position_text = _banner_parts(
            message.caption,
            3,
            'Для изображения нужна подпись: Название | Ссылка | Порядок',
        )
        image_url = ''
    else:
        title, image_url, target_url, position_text = _banner_parts(
            message.text,
            4,
            'Формат: Название | URL изображения | Ссылка | Порядок',
        )
        if image_url == '-' and banner:
            image_url = banner.image_url
        elif image_url:
            image_url = _valid_url(image_url, allow_local=True)

    if not title:
        raise ValueError('Название не может быть пустым')
    return (
        title,
        image_url,
        _valid_url(target_url, allow_local=True),
        int(position_text),
        upload,
    )


async def _save_banner_upload(file_id: str, suffix: str) -> str:
    BANNER_DIRECTORY.mkdir(parents=True, exist_ok=True)
    destination = BANNER_DIRECTORY / f'{uuid4().hex}{suffix}'
    await bot.get_bot().download(file_id, destination=destination)
    return f'/banners/{destination.name}'


@router.callback_query(BannerActionCallback.filter())
@transaction(1)
async def banner_action(
    callback: CallbackQuery,
    callback_data: BannerActionCallback,
    state: FSMContext,
):
    banner = (
        await Banner.get_by_id(callback_data.banner_id)
        if callback_data.banner_id else None
    )
    if callback_data.action in {'add', 'edit'}:
        if callback_data.action == 'edit' and not banner:
            return await callback.answer('Карточка не найдена', show_alert=True)
        await state.set_state(BannerForm.details)
        await state.update_data(
            banner_id=banner.id if banner and callback_data.action == 'edit' else 0,
            page=callback_data.page,
        )
        return await _edit_message(
            callback,
            _banner_prompt(banner if callback_data.action == 'edit' else None),
            _back_keyboard('banners', callback_data.page),
        )
    if not banner:
        return await callback.answer('Карточка не найдена', show_alert=True)
    if callback_data.action == 'toggle':
        banner.is_active = not banner.is_active
        banner.updated_at = datetime.now()
        banner.add()
        await _show_banners(callback, callback_data.page)
        return await callback.answer('Статус изменён')
    if callback_data.action == 'delete':
        return await _edit_message(
            callback,
            f'<b>Удалить карточку «{html.escape(banner.title)}»?</b>',
            inline_keyboard([
                ('🗑 Да, удалить', BannerActionCallback(action='confirm_delete', banner_id=banner.id, page=callback_data.page)),
                ('← Отмена', AdminSectionCallback(section='banners', page=callback_data.page)),
            ]),
        )
    if callback_data.action == 'confirm_delete':
        await banner.delete()
        await _show_banners(callback, callback_data.page)
        return await callback.answer('Карточка удалена')
    await callback.answer('Действие не найдено', show_alert=True)


@router.message(BannerForm.details)
@transaction(1)
async def banner_form(message: Message, state: FSMContext):
    data = await state.get_data()
    banner = await Banner.get_by_id(data.get('banner_id', 0))
    try:
        title, image_url, target_url, position, upload = _parse_banner_details(
            message,
            banner,
        )
        if upload:
            image_url = await _save_banner_upload(*upload)
    except (ValueError, TypeError) as exc:
        return await message.answer(
            f'Не удалось сохранить: {html.escape(str(exc))}\n\n{_banner_prompt(banner)}',
            reply_markup=_back_keyboard('banners', data.get('page', 0)),
        )

    if not banner:
        banner = Banner(title=title)
    banner.title = title
    banner.image_url = image_url
    banner.target_url = target_url
    banner.position = position
    banner.updated_at = datetime.now()
    banner.add()
    await session_context.get().flush()
    await state.clear()
    banners = await Banner.get_all()
    page = next((index for index, item in enumerate(banners) if item.id == banner.id), 0)
    await message.answer(
        _banner_text(banner),
        reply_markup=_banner_keyboard(banner, page, len(banners)),
    )


def _social_prompt(channel: SocialChannel | None = None) -> str:
    return (
        f'<b>{"Изменить" if channel else "Новая"} площадка</b>\n\n'
        'Отправьте одной строкой:\n'
        '<code>Telegram | Название канала | https://t.me/channel | 7 | 3 | @channel</code>\n\n'
        'Первое число — награда пригласившему, второе — самому подписавшемуся.\n\n'
        'Для Instagram, TikTok, YouTube и других площадок вместо последнего '
        'поля укажите <code>-</code> — такие подписки подтверждаются администратором. '
        'Для Telegram bot должен быть администратором канала.'
    )


@router.callback_query(SocialActionCallback.filter())
@transaction(1)
async def social_action(
    callback: CallbackQuery,
    callback_data: SocialActionCallback,
    state: FSMContext,
):
    channel = (
        await SocialChannel.get_by_id(callback_data.channel_id)
        if callback_data.channel_id else None
    )
    if callback_data.action in {'add', 'edit'}:
        if callback_data.action == 'edit' and not channel:
            return await callback.answer('Площадка не найдена', show_alert=True)
        await state.set_state(SocialForm.details)
        await state.update_data(
            channel_id=channel.id if channel and callback_data.action == 'edit' else 0,
            page=callback_data.page,
        )
        return await _edit_message(
            callback,
            _social_prompt(channel if callback_data.action == 'edit' else None),
            _back_keyboard('socials', callback_data.page),
        )
    if not channel:
        return await callback.answer('Площадка не найдена', show_alert=True)
    if callback_data.action == 'toggle':
        channel.is_active = not channel.is_active
        channel.updated_at = datetime.now()
        channel.add()
        await _show_socials(callback, callback_data.page)
        return await callback.answer('Статус изменён')
    await callback.answer('Действие не найдено', show_alert=True)


@router.message(SocialForm.details)
@transaction(1)
async def social_form(message: Message, state: FSMContext):
    data = await state.get_data()
    channel = await SocialChannel.get_by_id(data.get('channel_id', 0))
    parts = [part.strip() for part in (message.text or '').split('|')]
    try:
        if len(parts) not in {5, 6}:
            raise ValueError('Нужно шесть полей через |')
        if len(parts) == 5:
            platform, account_name, url, reward_text, telegram_chat_id = parts
            invitee_reward_text = str(channel.invitee_coin_reward if channel else 0)
        else:
            (
                platform,
                account_name,
                url,
                reward_text,
                invitee_reward_text,
                telegram_chat_id,
            ) = parts
        if not platform or not account_name:
            raise ValueError('Площадка и название аккаунта обязательны')
        url = _valid_url(url)
        coin_reward = whole_coin_reward(Decimal(reward_text.replace(',', '.')))
        invitee_coin_reward = whole_coin_reward(
            Decimal(invitee_reward_text.replace(',', '.'))
        )
        telegram_chat_id = None if telegram_chat_id == '-' else telegram_chat_id
        if platform.casefold() == 'telegram' and not telegram_chat_id:
            raise ValueError('Для автоматической проверки Telegram укажите @channel или chat ID')
    except (ValueError, InvalidOperation) as exc:
        return await message.answer(
            f'Не удалось сохранить: {html.escape(str(exc))}\n\n{_social_prompt()}',
            reply_markup=_back_keyboard('socials', data.get('page', 0)),
        )

    if not channel:
        channel = SocialChannel(
            platform=platform,
            account_name=account_name,
            url=url,
        )
    channel.platform = platform
    channel.account_name = account_name
    channel.url = url
    channel.coin_reward = coin_reward
    channel.invitee_coin_reward = invitee_coin_reward
    channel.telegram_chat_id = telegram_chat_id
    channel.updated_at = datetime.now()
    channel.add()
    await session_context.get().flush()
    await state.clear()
    channels = await SocialChannel.get_all()
    page = next((index for index, item in enumerate(channels) if item.id == channel.id), 0)
    await message.answer(
        _social_text(channel),
        reply_markup=_social_keyboard(channel, page, len(channels)),
    )


@router.callback_query(ReferralReviewCallback.filter())
@transaction(1)
async def referral_review_action(
    callback: CallbackQuery,
    callback_data: ReferralReviewCallback,
):
    reward = await ReferralReward.get_by_id(callback_data.reward_id)
    if not reward:
        return await callback.answer('Запись не найдена', show_alert=True)
    try:
        if callback_data.action == 'approve':
            changed = await approve_referral_reward(reward, callback.from_user.id)
            result = 'Подтверждено' if changed else 'Уже подтверждено'
        elif callback_data.action == 'reject':
            await reject_referral_reward(reward, callback.from_user.id)
            await bot.send_message(
                reward.invited_user_id,
                'Подписка пока не подтверждена. Проверьте её и отправьте запрос повторно.',
            )
            result = 'Возвращено на повторную проверку'
        else:
            return await callback.answer('Действие не найдено', show_alert=True)
    except ValueError as exc:
        return await callback.answer(str(exc), show_alert=True)
    await _show_referral_reviews(callback, callback_data.page)
    await callback.answer(result)


@router.callback_query(CoinSettingsCallback.filter())
@transaction(1)
async def coin_settings_action(
    callback: CallbackQuery,
    callback_data: CoinSettingsCallback,
    state: FSMContext,
):
    if callback_data.action == 'history':
        return await _show_coin_history(callback, callback_data.page)
    if callback_data.action == 'percent':
        await state.set_state(CoinSettingsForm.percent)
        return await _edit_message(
            callback,
            '<b>Процент коинов с покупки</b>\n\n'
            'Отправьте число от 0 до 100. Новое значение применяется только к заказам, '
            'оплата которых будет подтверждена после изменения.',
            _back_keyboard('coins'),
        )
    if callback_data.action == 'activation':
        await state.set_state(CoinSettingsForm.activation_reward)
        return await _edit_message(
            callback,
            '<b>Награда за первую реферальную активацию</b>\n\n'
            'Отправьте целое неотрицательное число. Значение 0 отключает начисление. '
            'Верхнего ограничения нет.',
            _back_keyboard('coins'),
        )
    if callback_data.action == 'adjust':
        user = (
            await User.get_by_id(callback_data.user_id)
            if callback_data.user_id else None
        )
        if callback_data.user_id and not user:
            return await callback.answer('Пользователь не найден', show_alert=True)
        await state.set_state(CoinSettingsForm.adjustment)
        await state.update_data(coin_adjustment_user_id=user.id if user else 0)
        if user:
            prompt = (
                f'<b>Изменение коинов пользователя {user.id}</b>\n\n'
                f'Текущий баланс: <b>{user.coin_balance}</b>\n'
                'Отправьте: <code>+100 | причина</code> или '
                '<code>-50 | причина</code>.'
            )
        else:
            prompt = (
                '<b>Ручное изменение коинов</b>\n\n'
                'Отправьте одной строкой:\n'
                '<code>username или Telegram ID | +100 или -50 | причина</code>'
            )
        return await _edit_message(callback, prompt, _back_keyboard('coins'))
    await callback.answer('Действие не найдено', show_alert=True)


@router.message(CoinSettingsForm.percent)
@transaction(1)
async def coin_settings_form(message: Message, state: FSMContext):
    try:
        value = Decimal((message.text or '').strip().replace(',', '.'))
        value = await set_purchase_coin_percent(value)
    except (InvalidOperation, ValueError) as exc:
        return await message.answer(
            f'Не удалось сохранить: {html.escape(str(exc))}',
            reply_markup=_back_keyboard('coins'),
        )
    await state.clear()
    await message.answer(
        f'Процент коинов с новых оплаченных заказов: <b>{value}%</b>.',
        reply_markup=_back_keyboard('coins'),
    )


@router.message(CoinSettingsForm.activation_reward)
@transaction(1)
async def coin_activation_reward_form(message: Message, state: FSMContext):
    try:
        value = Decimal((message.text or '').strip().replace(',', '.'))
        value = await set_referral_activation_reward(value)
    except (InvalidOperation, ValueError) as exc:
        return await message.answer(
            f'Не удалось сохранить: {html.escape(str(exc))}',
            reply_markup=_back_keyboard('coins'),
        )
    await state.clear()
    await message.answer(
        f'Награда за первую реферальную активацию: <b>{value} коинов</b>.',
        reply_markup=_back_keyboard('coins'),
    )


@router.message(CoinSettingsForm.adjustment)
@transaction(1)
async def coin_adjustment_form(message: Message, state: FSMContext):
    data = await state.get_data()
    fixed_user_id = int(data.get('coin_adjustment_user_id', 0))
    parts = [part.strip() for part in (message.text or '').split('|')]
    expected_parts = 2 if fixed_user_id else 3
    if len(parts) != expected_parts:
        example = (
            '<code>+100 | причина</code>' if fixed_user_id
            else '<code>username/ID | +100 | причина</code>'
        )
        return await message.answer(
            f'Неверный формат. Пример: {example}',
            reply_markup=_back_keyboard('coins'),
        )

    if fixed_user_id:
        user = await User.get_by_id(fixed_user_id)
        amount_text, reason = parts
    else:
        user = await User.find(parts[0])
        amount_text, reason = parts[1:]

    try:
        if not user:
            raise ValueError('Пользователь не найден')
        amount = Decimal(amount_text.replace(',', '.'))
        transaction = await adjust_user_coins(
            user,
            amount,
            reason,
            message.from_user.id,
        )
    except (InvalidOperation, ValueError) as exc:
        return await message.answer(
            f'Не удалось выполнить операцию: {html.escape(str(exc))}',
            reply_markup=_back_keyboard('coins'),
        )

    await state.clear()
    action = 'начислено' if transaction.amount > 0 else 'списано'
    await bot.send_message(
        user.id,
        f'<b>Баланс коинов изменён</b>\n\n'
        f'{action.capitalize()}: {abs(transaction.amount)}\n'
        f'Причина: {html.escape(reason)}\n'
        f'Новый баланс: {transaction.balance_after}',
    )
    await message.answer(
        f'Готово: пользователю <code>{user.id}</code> {action} '
        f'<b>{abs(transaction.amount)}</b> коинов.\n'
        f'Новый баланс: <b>{transaction.balance_after}</b>.',
        reply_markup=_user_keyboard(user.id),
    )


@plugin.setup()
def include_router(dispatcher: Dispatcher):
    dispatcher.include_router(router)
