import html
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.filters.command import CommandObject
from aiogram.types import CallbackQuery, Message, WebAppInfo
from pydantic import BaseModel
from rewire import config, simple_plugin
from rewire_sqlmodel import transaction

from src import bot
from src.catalog import CatalogValidationError, sync_catalog
from src.keyboards import inline_keyboard
from src.models import (
    AvailabilityRequest,
    Order,
    Product,
    Promocode,
    User,
)
from src.orders import cancel_order, confirm_payment, ORDER_STATUS_LABELS


@config
class Config(BaseModel):
    admin_chat_id: int
    mini_app_url: str
    products_path: str = 'assets/products.xlsx'


plugin = simple_plugin()
router = Router(name='admin')

router.message.filter(F.chat.id == Config.admin_chat_id)
router.callback_query.filter(F.message.chat.id == Config.admin_chat_id)


class AdminSectionCallback(CallbackData, prefix='adm'):
    section: str


class AvailabilityActionCallback(CallbackData, prefix='av'):
    request_id: int
    status: str


class OrderActionCallback(CallbackData, prefix='ord'):
    order_id: int
    action: str


class UserSectionCallback(CallbackData, prefix='usr'):
    user_id: int
    section: str


class PromocodeToggleCallback(CallbackData, prefix='promo'):
    promocode_id: int


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
            text,
            AvailabilityActionCallback(
                request_id=request_id, status=status,
            ),
        )
        for text, status in (
            ('✅ Есть всё', 'available'),
            ('❌ Нет', 'unavailable'),
            ('⏳ Уточняется', 'on_request'),
        )
    ]
    buttons.append(('⌂ Меню', AdminSectionCallback(section='menu')))
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
        f'Покупатель: {user.id} ({username})',
        reply_markup=inline_keyboard([
            (
                '✅ Подтвердить оплату',
                OrderActionCallback(order_id=order.id, action='paid'),
            ),
            (
                '✖️ Отменить заказ',
                OrderActionCallback(order_id=order.id, action='cancel'),
            ),
            ('⌂ Меню', AdminSectionCallback(section='menu')),
        ], 1, 1, 1),
    )


ADMIN_TEXT = (
    '<b>Администрирование Fyvessa</b>\n\n'
    'Выберите очередь или действие. Товары редактируются в Excel, остальные '
    'операции выполняются здесь.'
)


async def _edit_message(callback: CallbackQuery, text: str, reply_markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if 'message is not modified' not in str(exc):
            raise


def _admin_keyboard():
    return inline_keyboard([
        (text, AdminSectionCallback(section=section))
        for text, section in (
            ('📦 Запросы наличия', 'availability'),
            ('💳 Заказы и оплаты', 'orders'),
            ('👥 Пользователи', 'users'),
            ('🎟 Промокоды', 'promos'),
            ('📊 Сводка', 'summary'),
            ('🔄 Синхронизировать Excel', 'sync'),
            ('❓ Команды и подсказки', 'help'),
        )
    ])


def _page_section(section: str, page: int = 0) -> str:
    return f'{section}.{page}' if page else section


def _parse_section(value: str) -> tuple[str, int]:
    section, separator, page = value.rpartition('.')
    if separator and page.isdigit():
        return section, int(page)

    return value, 0


def _back_keyboard(section: str = 'menu', page: int = 0):
    if section == 'menu':
        return inline_keyboard([
            ('⌂ Меню', AdminSectionCallback(section='menu')),
        ])

    section_labels = {
        'availability': '← К запросам',
        'orders': '← К заказам',
        'promos': '← К промокодам',
        'users': '← К пользователям',
    }
    return inline_keyboard([
        (
            section_labels.get(section, '← К разделу'),
            AdminSectionCallback(section=_page_section(section, page)),
        ),
        ('⌂ Меню', AdminSectionCallback(section='menu')),
    ])


async def _answer_with_navigation(message: Message, text: str, section: str = 'menu'):
    return await message.answer(text, reply_markup=_back_keyboard(section))


def _admin_navigation(section: str, page: int, total: int) -> list[tuple[str, Any]]:
    buttons = []
    if page > 0:
        buttons.append((
            f'← {page}/{total}',
            AdminSectionCallback(section=_page_section(section, page - 1)),
        ))

    if page + 1 < total:
        buttons.append((
            f'{page + 2}/{total} →',
            AdminSectionCallback(section=_page_section(section, page + 1)),
        ))

    return buttons + [('⌂ Меню', AdminSectionCallback(section='menu'))]


def _user_navigation(
    user_id: int, section: str, page: int, total: int,
) -> list[tuple[str, Any]]:
    buttons = []
    if page > 0:
        buttons.append((
            f'← {page}/{total}',
            UserSectionCallback(
                user_id=user_id, section=_page_section(section, page - 1),
            ),
        ))

    if page + 1 < total:
        buttons.append((
            f'{page + 2}/{total} →',
            UserSectionCallback(
                user_id=user_id, section=_page_section(section, page + 1),
            ),
        ))

    return buttons + [
        (
            '← Пользователь',
            UserSectionCallback(user_id=user_id, section='card'),
        ),
        ('⌂ Меню', AdminSectionCallback(section='menu')),
    ]


def _help_text() -> str:
    return (
        '<b>Команды администратора</b>\n\n'
        '/admin — панель с очередями и сводкой\n'
        '/user &lt;username/Telegram ID&gt; — карточка пользователя\n'
        '/discount &lt;username/ID&gt; &lt;0–100&gt; — персональная скидка\n'
        '/availability &lt;ID&gt; &lt;есть|нет|уточняется&gt; '
        '[количество] [комментарий]\n'
        '/order &lt;ID&gt; &lt;оплачен|отмена&gt; — изменить заказ\n'
        '/promo CODE | Партнёр | скидка | вознаграждение — создать или обновить\n'
        '/promo_toggle CODE — включить или выключить промокод\n'
        '/sync_products — синхронизировать assets/products.xlsx\n\n'
        'Также поддерживаются значения available, unavailable, on_request, paid и cancel.\n\n'
        'Безопасность: команды и кнопки работают только в настроенном админском чате.'
    )


@router.message(Command('admin'))
async def admin_menu(message: Message):
    await message.answer(ADMIN_TEXT, reply_markup=_admin_keyboard())


async def _sync_text() -> str:
    try:
        report = await sync_catalog(Config.products_path)
    except CatalogValidationError as exc:
        return (
            '❌ <b>Excel не импортирован</b>\n'
            f'<pre>{html.escape(str(exc))}</pre>'
        )

    return (
        '✅ <b>Каталог синхронизирован</b>\n\n'
        f'Создано товаров: {report.products_created}\n'
        f'Обновлено товаров: {report.products_updated}\n'
        f'Скрыто товаров: {report.products_hidden}\n'
        f'Создано категорий: {report.categories_created}'
    )


@router.message(Command('sync_products'))
async def sync_products_command(message: Message):
    await _answer_with_navigation(message, await _sync_text())


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
            ('🔄 Обновить карточку', 'card'),
        )
    ]

    if page > 0:
        buttons.append((
            f'← {page}/{total}',
            AdminSectionCallback(
                section=_page_section('users', page - 1),
            ),
        ))

    if page + 1 < total:
        buttons.append((
            f'{page + 2}/{total} →',
            AdminSectionCallback(
                section=_page_section('users', page + 1),
            ),
        ))

    buttons.append(('⌂ Меню', AdminSectionCallback(section='menu')))
    return inline_keyboard(buttons, 2, 1, 2, 1)


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
        f'{order.created_at:%d.%m.%Y %H:%M}\n'
        f'Пользователь: <code>{order.user_id}</code>{username}'
    )


def _order_keyboard(
    order: Order, *, user_id: int = 0, page: int = 0, total: int = 1,
):
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


def _availability_admin_keyboard(
    availability: AvailabilityRequest, *, user_id: int = 0, page: int = 0,
    total: int = 1,
):
    buttons = (
        [
            (
                text,
                AvailabilityActionCallback(request_id=availability.id, status=status),
            )
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


def _promocode_text(promocode: Promocode) -> str:
    return (
        f'<b>{html.escape(promocode.code)}</b> · {html.escape(promocode.partner_name)}\n'
        f'Скидка {promocode.user_discount_percent}% · '
        f'вознаграждение {promocode.partner_reward_percent}% · '
        f'{'активен' if promocode.is_active else 'отключён'}'
    )


def _promocode_keyboard(promocode: Promocode, page: int, total: int):
    return inline_keyboard([
        (
            'Выключить' if promocode.is_active else 'Включить',
            PromocodeToggleCallback(promocode_id=promocode.id),
        ),
        *_admin_navigation('promos', page, total),
    ], 1, 2, 1)


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
            f'{AVAILABILITY_LABELS[status]}.{suffix_text}{note_text}',
            reply_markup=_cart_keyboard() if status == 'available' else None,
        )

    return True


@router.callback_query(AdminSectionCallback.filter())
@transaction(1)
async def admin_section(callback: CallbackQuery, callback_data: AdminSectionCallback):
    section, page = _parse_section(callback_data.section)
    if section == 'menu':
        await _edit_message(callback, ADMIN_TEXT, _admin_keyboard())
    elif section == 'sync':
        await _edit_message(callback, await _sync_text(), _back_keyboard())
    elif section == 'help':
        await _edit_message(callback, _help_text(), _back_keyboard())
    elif section == 'availability':
        requests = await AvailabilityRequest.get_recent(pending_only=True)
        if not requests:
            return await callback.answer('Запросов наличия нет.', show_alert=True)
        page = min(page, len(requests) - 1)
        availability = requests[page]
        await _edit_message(
            callback, await _availability_text(availability),
            _availability_admin_keyboard(
                availability, page=page, total=len(requests),
            ),
        )
    elif section == 'orders':
        orders = await Order.get_recent()
        if not orders:
            return await callback.answer('Заказов пока нет.', show_alert=True)
        page = min(page, len(orders) - 1)
        order = orders[page]
        await _edit_message(
            callback, _order_text(order, await User.get_by_id(order.user_id)),
            _order_keyboard(order, page=page, total=len(orders)),
        )
    elif section == 'users':
        users = await User.get_recent()
        if not users:
            return await callback.answer('Пользователей пока нет.', show_alert=True)
        page = min(page, len(users) - 1)
        user = users[page]
        await _edit_message(
            callback, await _user_text(user),
            _user_keyboard(user.id, page, len(users)),
        )
    elif section == 'promos':
        promocodes = await Promocode.get_recent()
        if not promocodes:
            return await callback.answer(
                'Промокодов нет. Создание:\n'
                '/promo CODE | Партнёр | Процент скидки | Процент вознаграждения',
                show_alert=True,
            )

        page = min(page, len(promocodes) - 1)
        promocode = promocodes[page]
        await _edit_message(
            callback, _promocode_text(promocode),
            _promocode_keyboard(promocode, page, len(promocodes)),
        )

    elif section == 'summary':
        products = await Product.get_all()
        users = await User.get_all()
        orders = await Order.get_all()
        pending = await AvailabilityRequest.get_recent(
            pending_only=True, limit=None,
        )
        review = [order for order in orders if order.status == 'payment_review']
        paid_total = sum(
            (order.paid_total for order in orders if order.payment_status == 'paid'),
            Decimal('0'),
        )
        await _edit_message(
            callback,
            '<b>Текущая сводка</b>\n\n'
            f'Активных товаров: {sum(product.is_active for product in products)}\n'
            f'Пользователей: {len(users)}\n'
            f'Заказов: {len(orders)}\n'
            f'Оплат на проверке: {len(review)}\n'
            f'Запросов наличия без ответа: {len(pending)}\n'
            f'Подтверждено оплат: {paid_total} ₽',
            _back_keyboard(),
        )
    else:
        return await callback.answer('Раздел не найден.', show_alert=True)


@router.callback_query(UserSectionCallback.filter())
@transaction(1)
async def user_section(callback: CallbackQuery, callback_data: UserSectionCallback):
    user = await User.get_by_id(callback_data.user_id)
    if not user:
        return await callback.answer('Пользователь не найден', show_alert=True)

    section, page = _parse_section(callback_data.section)
    if section == 'orders':
        orders = await Order.get_recent(user_id=user.id, limit=10)
        if not orders:
            return await callback.answer('У пользователя пока нет заказов.', show_alert=True)

        page = min(page, len(orders) - 1)
        order = orders[page]
        await _edit_message(
            callback, _order_text(order, user),
            _order_keyboard(
                order, user_id=user.id, page=page, total=len(orders),
            ),
        )

    elif section == 'availability':
        requests = await AvailabilityRequest.get_recent(user_id=user.id)
        if not requests:
            return await callback.answer('Запросов наличия нет.', show_alert=True)

        page = min(page, len(requests) - 1)
        availability = requests[page]
        await _edit_message(
            callback, await _availability_text(availability),
            _availability_admin_keyboard(
                availability, user_id=user.id, page=page, total=len(requests),
            ),
        )
    else:
        await _edit_message(callback, await _user_text(user), _user_keyboard(user.id))


@router.callback_query(AvailabilityActionCallback.filter())
@transaction(1)
async def availability_action(callback: CallbackQuery, callback_data: AvailabilityActionCallback):
    availability = await AvailabilityRequest.get_by_id(callback_data.request_id)
    if not availability:
        return await callback.answer('Запрос не найден', show_alert=True)

    changed = await _resolve_availability(
        availability,
        callback_data.status,
        None,
        None,
        callback.from_user.id
    )

    if not changed:
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
        changed = await _resolve_availability(
            availability,
            AVAILABILITY_COMMAND_STATUSES.get(
                parts[1].casefold(), parts[1].casefold(),
            ),
            quantity,
            comment,
            message.from_user.id
        )
    except ValueError as exc:
        return await _answer_with_navigation(
            message, html.escape(str(exc)), 'availability',
        )

    await _answer_with_navigation(
        message,
        'Ответ отправлен.' if changed else 'Запрос уже обработан.',
        'availability',
    )


async def _apply_order_action(order: Order, action: str, admin_id: int) -> bool:
    if action == 'paid':
        changed = await confirm_payment(order, admin_id)
        text = f'✅ Оплата заказа <b>{html.escape(order.number)}</b> подтверждена.'
    elif action == 'cancel':
        changed = await cancel_order(order, admin_id)
        text = f'Заказ <b>{html.escape(order.number)}</b> отменён.'
    else:
        raise ValueError('Действие: оплачен или отмена')

    if changed:
        user = await User.get_by_id(order.user_id)
        if user:
            await bot.send_message(user.id, text)

    return changed


@router.callback_query(OrderActionCallback.filter())
@transaction(1)
async def order_action(callback: CallbackQuery, callback_data: OrderActionCallback):
    order = await Order.get_by_id(callback_data.order_id)
    if not order:
        return await callback.answer('Заказ не найден', show_alert=True)

    try:
        changed = await _apply_order_action(
            order,
            callback_data.action,
            callback.from_user.id,
        )
    except ValueError as exc:
        return await callback.answer(str(exc), show_alert=True)

    if not changed:
        return await callback.answer('Уже выполнено', show_alert=True)

    user = await User.get_by_id(order.user_id)
    orders = await Order.get_recent()
    page = next(
        (index for index, item in enumerate(orders) if item.id == order.id), 0,
    )

    await _edit_message(
        callback, _order_text(order, user),
        _order_keyboard(
            order, page=page, total=max(len(orders), 1),
        ),
    )

    await callback.answer('Готово')


@router.message(Command('order'))
@transaction(1)
async def order_command(message: Message, command: CommandObject):
    parts = (command.args or '').split()
    if len(parts) != 2 or not parts[0].isdigit():
        return await _answer_with_navigation(
            message,
            'Формат: <code>/order ID оплачен</code> или <code>/order ID отмена</code>',
            'orders',
        )

    order = await Order.get_by_id(int(parts[0]))
    if not order:
        return await _answer_with_navigation(
            message, 'Заказ не найден.', 'orders',
        )

    try:
        action = ORDER_COMMAND_ACTIONS.get(
            parts[1].casefold(), parts[1].casefold(),
        )
        changed = await _apply_order_action(order, action, message.from_user.id)
    except ValueError as exc:
        return await _answer_with_navigation(
            message, html.escape(str(exc)), 'orders',
        )

    await _answer_with_navigation(
        message,
        'Готово.' if changed else 'Это действие уже выполнено.',
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
async def promocode_command(message: Message, command: CommandObject):
    parts = [part.strip() for part in (command.args or '').split('|')]
    if len(parts) != 4:
        return await _answer_with_navigation(
            message,
            'Формат: <code>/promo CODE | Имя партнёра | Процент скидки | '
            'Процент вознаграждения</code>',
            'promos',
        )

    try:
        user_percent = Decimal(parts[2].replace(',', '.'))
        partner_percent = Decimal(parts[3].replace(',', '.'))
    except InvalidOperation:
        return await _answer_with_navigation(
            message, 'Проценты должны быть числами.', 'promos',
        )

    if not all((parts[0], parts[1])) or not all(
        Decimal('0') <= value <= Decimal('100')
        for value in (user_percent, partner_percent)
    ):
        return await _answer_with_navigation(
            message,
            'Проверьте код, имя и диапазон процентов 0–100.',
            'promos',
        )

    code = parts[0].upper()
    promocode = await Promocode.get_by_code(code)
    if not promocode:
        promocode = Promocode(code=code, partner_name=parts[1])

    promocode.partner_name = parts[1]
    promocode.user_discount_percent = user_percent
    promocode.partner_reward_percent = partner_percent
    promocode.is_active = True
    promocode.add()

    await _answer_with_navigation(
        message,
        f'Промокод <b>{html.escape(code)}</b> сохранён и активен.',
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


@router.callback_query(PromocodeToggleCallback.filter())
@transaction(1)
async def promocode_toggle_callback(
    callback: CallbackQuery, callback_data: PromocodeToggleCallback,
):
    promocode = await Promocode.get_by_id(callback_data.promocode_id)
    if not promocode:
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
        callback, _promocode_text(promocode),
        _promocode_keyboard(promocode, page, max(len(promocodes), 1)),
    )

    await callback.answer('Включён' if promocode.is_active else 'Отключён')


@plugin.setup()
def include_router(dispatcher: Dispatcher):
    dispatcher.include_router(router)
