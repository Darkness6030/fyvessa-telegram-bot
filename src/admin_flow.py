import html
from datetime import datetime
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.filters.command import CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pydantic import BaseModel
from rewire import config, logger, simple_plugin
from rewire_sqlmodel import transaction
from sqlalchemy import func

from src.catalog import CatalogValidationError, sync_catalog
from src.models import (
    AvailabilityRequest,
    Order,
    Product,
    PromoCode,
    User,
)
from src.orders import ORDER_STATUS_LABELS, cancel_order, confirm_payment


@config
class Config(BaseModel):
    admin_chat_id: str = ''
    products_path: str = 'assets/products.xlsx'


plugin = simple_plugin()
router = Router(name='admin')


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


class PromoToggleCallback(CallbackData, prefix='promo'):
    promo_id: int


AVAILABILITY_LABELS = {
    'pending': 'Ожидает ответа',
    'available': 'Есть в наличии',
    'unavailable': 'Нет в наличии',
    'on_request': 'Под заказ / уточняется',
    'used': 'Использовано в заказе',
}


def _availability_keyboard(request_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text='✅ Есть всё',
        callback_data=AvailabilityActionCallback(
            request_id=request_id,
            status='available',
        ),
    )
    builder.button(
        text='❌ Нет',
        callback_data=AvailabilityActionCallback(
            request_id=request_id,
            status='unavailable',
        ),
    )
    builder.button(
        text='⏳ Уточняется',
        callback_data=AvailabilityActionCallback(
            request_id=request_id,
            status='on_request',
        ),
    )
    return builder.adjust(2, 1).as_markup()


async def notify_availability_request(
    bot: Bot,
    availability: AvailabilityRequest,
    product: Product,
    user: User,
) -> bool:
    chat_id = admin_chat_id()
    if chat_id is None:
        logger.error('ADMIN_CHAT_ID is not configured; availability notification skipped')
        return False
    username = f'@{html.escape(user.username)}' if user.username else 'без username'
    try:
        await bot.send_message(
            chat_id,
            f'<b>Новый запрос наличия №{availability.id}</b>\n\n'
            f'Товар: <b>{html.escape(product.name)}</b>\n'
            f'SKU: <code>{html.escape(product.sku)}</code>\n'
            f'Нужно: <b>{availability.requested_quantity or 1} шт.</b>\n'
            f'Покупатель: {user.id} ({username})',
            reply_markup=_availability_keyboard(availability.id),
        )
    except Exception as exc:
        logger.error('Failed to notify admin chat about availability: {}', exc)
        return False
    return True


async def notify_payment_review(bot: Bot, order: Order, user: User) -> bool:
    chat_id = admin_chat_id()
    if chat_id is None:
        logger.error('ADMIN_CHAT_ID is not configured; payment notification skipped')
        return False
    builder = InlineKeyboardBuilder()
    builder.button(
        text='✅ Подтвердить оплату',
        callback_data=OrderActionCallback(order_id=order.id, action='paid'),
    )
    builder.button(
        text='✖️ Отменить заказ',
        callback_data=OrderActionCallback(order_id=order.id, action='cancel'),
    )
    username = f'@{html.escape(user.username)}' if user.username else 'без username'
    try:
        await bot.send_message(
            chat_id,
            f'<b>Покупатель сообщил об оплате</b>\n\n'
            f'Заказ: <b>{html.escape(order.number)}</b>\n'
            f'Сумма: <b>{order.paid_total} ₽</b>\n'
            f'Покупатель: {user.id} ({username})',
            reply_markup=builder.adjust(1).as_markup(),
        )
    except Exception as exc:
        logger.error('Failed to notify admin chat about payment: {}', exc)
        return False
    return True


def admin_chat_id() -> int | None:
    value = Config.admin_chat_id.strip()
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _is_admin_chat(chat_id: int) -> bool:
    configured_chat_id = admin_chat_id()
    return configured_chat_id is not None and chat_id == configured_chat_id


async def _deny(message: Message):
    await message.answer('Команда доступна только в админском чате.')


def _admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(
        text='📦 Запросы наличия',
        callback_data=AdminSectionCallback(section='availability'),
    )
    builder.button(
        text='💳 Заказы и оплаты',
        callback_data=AdminSectionCallback(section='orders'),
    )
    builder.button(
        text='👥 Пользователи',
        callback_data=AdminSectionCallback(section='users'),
    )
    builder.button(
        text='🎟 Промокоды',
        callback_data=AdminSectionCallback(section='promos'),
    )
    builder.button(
        text='📊 Сводка',
        callback_data=AdminSectionCallback(section='summary'),
    )
    builder.button(
        text='🔄 Синхронизировать Excel',
        callback_data=AdminSectionCallback(section='sync'),
    )
    builder.button(
        text='❓ Команды и подсказки',
        callback_data=AdminSectionCallback(section='help'),
    )
    return builder.adjust(1).as_markup()


def _help_text() -> str:
    return (
        '<b>Команды администратора</b>\n\n'
        '/admin — панель с очередями и сводкой\n'
        '/user &lt;username/Telegram ID&gt; — карточка пользователя\n'
        '/discount &lt;username/ID&gt; &lt;0–100&gt; — персональная скидка\n'
        '/availability &lt;ID&gt; &lt;available|unavailable|on_request&gt; '
        '[количество] [комментарий]\n'
        '/order &lt;ID&gt; &lt;paid|cancel&gt; — изменить заказ\n'
        '/promo CODE | Партнёр | скидка | вознаграждение — создать или обновить\n'
        '/promo_toggle CODE — включить или выключить промокод\n'
        '/sync_products — синхронизировать assets/products.xlsx\n\n'
        'Безопасность: команды и кнопки работают только в настроенном админском чате.'
    )


@router.message(Command('admin'))
async def admin_menu(message: Message):
    if not _is_admin_chat(message.chat.id):
        return await _deny(message)
    await message.answer(
        '<b>Администрирование Fyvessa</b>\n\n'
        'Выберите очередь или действие. Товары редактируются в Excel, остальные '
        'операции выполняются здесь.',
        reply_markup=_admin_keyboard(),
    )


async def _run_sync(message: Message):
    try:
        report = await sync_catalog(Config.products_path)
    except CatalogValidationError as exc:
        await message.answer(
            f'❌ <b>Excel не импортирован</b>\n<pre>{html.escape(str(exc))}</pre>'
        )
        return
    await message.answer(
        '✅ <b>Каталог синхронизирован</b>\n\n'
        f'Создано товаров: {report.created}\n'
        f'Обновлено товаров: {report.updated}\n'
        f'Скрыто товаров: {report.hidden}\n'
        f'Создано категорий: {report.categories_created}'
    )


@router.message(Command('sync_products'))
async def sync_products_command(message: Message):
    if not _is_admin_chat(message.chat.id):
        return await _deny(message)
    await _run_sync(message)


async def _find_user(value: str) -> User | None:
    value = value.strip().lstrip('@')
    if not value:
        return None
    if value.isdigit():
        return await User.select().filter_by(id=int(value)).first()
    return await (
        User.select()
        .where(func.lower(User.username) == value.lower())
        .first()
    )


async def _user_text(user: User) -> str:
    orders = list(await Order.select().filter_by(user_id=user.id).all())
    requests = list(
        await AvailabilityRequest.select().filter_by(user_id=user.id).all()
    )
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
        f'Телефон: {html.escape(user.phone_number or "не указан")}\n'
        f'Дата рождения: {user.birth_date or "не указана"}\n'
        f'Регистрация: {"✅ заполнена" if user.is_registered else "⚠️ не завершена"}\n'
        f'Коины: {user.coin_balance}\n'
        f'Персональная скидка: {user.personal_discount_percent}%\n'
        f'Пригласил: {user.referrer_id or "нет"}\n'
        f'Заказов: {len(orders)}\n'
        f'Запросов наличия: {len(requests)}\n'
        f'Создан: {user.created_at:%d.%m.%Y}'
    )


def _user_keyboard(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text='📦 Заказы',
        callback_data=UserSectionCallback(user_id=user_id, section='orders'),
    )
    builder.button(
        text='🔎 Запросы наличия',
        callback_data=UserSectionCallback(user_id=user_id, section='availability'),
    )
    builder.button(
        text='🔄 Обновить карточку',
        callback_data=UserSectionCallback(user_id=user_id, section='card'),
    )
    return builder.adjust(2, 1).as_markup()


@router.message(Command('user'))
@transaction(1)
async def user_command(message: Message, command: CommandObject):
    if not _is_admin_chat(message.chat.id):
        return await _deny(message)
    user = await _find_user(command.args or '')
    if not user:
        await message.answer(
            'Пользователь не найден. Используйте <code>/user username</code> или '
            '<code>/user 123456789</code>.'
        )
        return
    await message.answer(
        await _user_text(user),
        reply_markup=_user_keyboard(user.id),
    )


async def _send_user_orders(message: Message, user: User):
    orders = list(
        await Order.select()
        .filter_by(user_id=user.id)
        .order_by(Order.created_at.desc())
        .all()
    )[:10]
    if not orders:
        await message.answer('У пользователя пока нет заказов.')
        return
    for order in orders:
        builder = InlineKeyboardBuilder()
        if order.status == 'payment_review':
            builder.button(
                text='✅ Подтвердить оплату',
                callback_data=OrderActionCallback(order_id=order.id, action='paid'),
            )
        if order.payment_status != 'paid' and order.status != 'cancelled':
            builder.button(
                text='✖️ Отменить',
                callback_data=OrderActionCallback(order_id=order.id, action='cancel'),
            )
        await message.answer(
            f'<b>{html.escape(order.number)}</b> · {order.paid_total} ₽\n'
            f'{ORDER_STATUS_LABELS.get(order.status, order.status)} · {order.created_at:%d.%m.%Y %H:%M}',
            reply_markup=builder.adjust(1).as_markup() if list(builder.buttons) else None,
        )


async def _send_availability_requests(
    message: Message,
    user_id: int | None = None,
):
    query = AvailabilityRequest.select()
    if user_id is None:
        query = query.filter_by(status='pending')
    else:
        query = query.filter_by(user_id=user_id)
    requests = list(await query.order_by(AvailabilityRequest.created_at.desc()).all())[:15]
    if not requests:
        await message.answer('Запросов наличия нет.')
        return
    for availability in requests:
        product = await Product.select().filter_by(id=availability.product_id).first()
        user = await User.select().filter_by(id=availability.user_id).first()
        builder = InlineKeyboardBuilder()
        if availability.status == 'pending':
            builder.button(
                text='✅ Есть',
                callback_data=AvailabilityActionCallback(
                    request_id=availability.id,
                    status='available',
                ),
            )
            builder.button(
                text='❌ Нет',
                callback_data=AvailabilityActionCallback(
                    request_id=availability.id,
                    status='unavailable',
                ),
            )
            builder.button(
                text='⏳ Уточняется',
                callback_data=AvailabilityActionCallback(
                    request_id=availability.id,
                    status='on_request',
                ),
            )
        await message.answer(
            f'<b>Запрос №{availability.id}</b> · '
            f'{AVAILABILITY_LABELS.get(availability.status, availability.status)}\n'
            f'Товар: {html.escape(product.name if product else "удалён")}\n'
            f'Количество: {availability.requested_quantity or 1}\n'
            f'Пользователь: {user.id} '
            f'(@{html.escape(user.username) if user and user.username else "без username"})'
            if user
            else f'<b>Запрос №{availability.id}</b> · пользователь удалён',
            reply_markup=builder.adjust(2, 1).as_markup() if list(builder.buttons) else None,
        )


async def _resolve_availability(
    availability: AvailabilityRequest,
    status: str,
    available_quantity: int | None,
    comment: str | None,
    admin_id: int,
    bot: Bot,
) -> bool:
    if availability.status != 'pending':
        return False
    if status not in {'available', 'unavailable', 'on_request'}:
        raise ValueError('Неизвестный статус')
    if status == 'available':
        available_quantity = available_quantity or availability.requested_quantity or 1
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

    user = await User.select().filter_by(id=availability.user_id).first()
    product = await Product.select().filter_by(id=availability.product_id).first()
    if user:
        suffix = (
            f' Доступно: {available_quantity} шт.'
            if status == 'available' and available_quantity is not None
            else ''
        )
        note = f'\nКомментарий: {html.escape(comment)}' if comment else ''
        try:
            await bot.send_message(
                user.id,
                f'<b>Ответ по наличию</b>\n\n'
                f'{html.escape(product.name if product else "Товар")}: '
                f'{AVAILABILITY_LABELS[status]}.{suffix}{note}',
            )
        except Exception:
            pass
    return True


@router.callback_query(AdminSectionCallback.filter())
@transaction(1)
async def admin_section(
    callback: CallbackQuery,
    callback_data: AdminSectionCallback,
):
    if not _is_admin_chat(callback.message.chat.id):
        return await callback.answer('Недостаточно прав', show_alert=True)
    section = callback_data.section
    if section == 'sync':
        await _run_sync(callback.message)
    elif section == 'help':
        await callback.message.answer(_help_text())
    elif section == 'availability':
        await _send_availability_requests(callback.message)
    elif section == 'orders':
        users = list(await User.select().all())
        users_by_id = {user.id: user for user in users}
        orders = list(await Order.select().order_by(Order.created_at.desc()).all())[:15]
        if not orders:
            await callback.message.answer('Заказов пока нет.')
        for order in orders:
            user = users_by_id.get(order.user_id)
            builder = InlineKeyboardBuilder()
            if order.status == 'payment_review':
                builder.button(
                    text='✅ Подтвердить оплату',
                    callback_data=OrderActionCallback(order_id=order.id, action='paid'),
                )
            if order.payment_status != 'paid' and order.status != 'cancelled':
                builder.button(
                    text='✖️ Отменить',
                    callback_data=OrderActionCallback(order_id=order.id, action='cancel'),
                )
            await callback.message.answer(
                f'<b>{html.escape(order.number)}</b> · {order.paid_total} ₽\n'
                f'{ORDER_STATUS_LABELS.get(order.status, order.status)}\n'
                f'Пользователь: {order.user_id}'
                f'{f" (@{html.escape(user.username)})" if user and user.username else ""}',
                reply_markup=builder.adjust(1).as_markup() if list(builder.buttons) else None,
            )
    elif section == 'users':
        users = list(
            await User.select().order_by(User.created_at.desc()).all()
        )[:15]
        if not users:
            await callback.message.answer('Пользователей пока нет.')
        for user in users:
            await callback.message.answer(
                await _user_text(user),
                reply_markup=_user_keyboard(user.id),
            )
    elif section == 'promos':
        promos = list(await PromoCode.select().order_by(PromoCode.created_at.desc()).all())
        if not promos:
            await callback.message.answer(
                'Промокодов нет. Создание:\n'
                '<code>/promo CODE | Партнёр | 10 | 10</code>'
            )
        for promo in promos:
            builder = InlineKeyboardBuilder().button(
                text='Выключить' if promo.is_active else 'Включить',
                callback_data=PromoToggleCallback(promo_id=promo.id),
            )
            await callback.message.answer(
                f'<b>{html.escape(promo.code)}</b> · {html.escape(promo.partner_name)}\n'
                f'Скидка {promo.user_discount_percent}% · '
                f'вознаграждение {promo.partner_reward_percent}% · '
                f'{"активен" if promo.is_active else "отключён"}',
                reply_markup=builder.as_markup(),
            )
    elif section == 'summary':
        products = list(await Product.select().all())
        users = list(await User.select().all())
        orders = list(await Order.select().all())
        pending = list(await AvailabilityRequest.select().filter_by(status='pending').all())
        review = [order for order in orders if order.status == 'payment_review']
        paid_total = sum(
            (order.paid_total for order in orders if order.payment_status == 'paid'),
            Decimal('0'),
        )
        await callback.message.answer(
            '<b>Текущая сводка</b>\n\n'
            f'Активных товаров: {sum(product.is_active for product in products)}\n'
            f'Пользователей: {len(users)}\n'
            f'Заказов: {len(orders)}\n'
            f'Оплат на проверке: {len(review)}\n'
            f'Запросов наличия без ответа: {len(pending)}\n'
            f'Подтверждено оплат: {paid_total} ₽'
        )


@router.callback_query(UserSectionCallback.filter())
@transaction(1)
async def user_section(
    callback: CallbackQuery,
    callback_data: UserSectionCallback,
):
    if not _is_admin_chat(callback.message.chat.id):
        return await callback.answer('Недостаточно прав', show_alert=True)
    user = await User.select().filter_by(id=callback_data.user_id).first()
    if not user:
        return await callback.answer('Пользователь не найден', show_alert=True)
    if callback_data.section == 'orders':
        await _send_user_orders(callback.message, user)
    elif callback_data.section == 'availability':
        await _send_availability_requests(callback.message, user.id)
    else:
        await callback.message.answer(
            await _user_text(user),
            reply_markup=_user_keyboard(user.id),
        )


@router.callback_query(AvailabilityActionCallback.filter())
@transaction(1)
async def availability_action(
    callback: CallbackQuery,
    callback_data: AvailabilityActionCallback,
    bot: Bot,
):
    if not _is_admin_chat(callback.message.chat.id):
        return await callback.answer('Недостаточно прав', show_alert=True)
    availability = await AvailabilityRequest.select().filter_by(
        id=callback_data.request_id
    ).first()
    if not availability:
        return await callback.answer('Запрос не найден', show_alert=True)
    changed = await _resolve_availability(
        availability,
        callback_data.status,
        None,
        None,
        callback.from_user.id,
        bot,
    )
    await callback.answer('Ответ отправлен' if changed else 'Запрос уже обработан')
    if changed:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.message(Command('availability'))
@transaction(1)
async def availability_command(
    message: Message,
    command: CommandObject,
    bot: Bot,
):
    if not _is_admin_chat(message.chat.id):
        return await _deny(message)
    parts = (command.args or '').split(maxsplit=3)
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer(
            'Формат: <code>/availability ID available 3 комментарий</code>'
        )
        return
    quantity = None
    comment = None
    if len(parts) >= 3:
        if parts[2].isdigit():
            quantity = int(parts[2])
            comment = parts[3] if len(parts) == 4 else None
        else:
            comment = ' '.join(parts[2:])
    availability = await AvailabilityRequest.select().filter_by(id=int(parts[0])).first()
    if not availability:
        await message.answer('Запрос не найден.')
        return
    try:
        changed = await _resolve_availability(
            availability,
            parts[1],
            quantity,
            comment,
            message.from_user.id,
            bot,
        )
    except ValueError as exc:
        await message.answer(html.escape(str(exc)))
        return
    await message.answer('Ответ отправлен.' if changed else 'Запрос уже обработан.')


async def _apply_order_action(order: Order, action: str, admin_id: int, bot: Bot) -> bool:
    if action == 'paid':
        changed = await confirm_payment(order, admin_id)
        text = f'✅ Оплата заказа <b>{html.escape(order.number)}</b> подтверждена.'
    elif action == 'cancel':
        changed = await cancel_order(order, admin_id)
        text = f'Заказ <b>{html.escape(order.number)}</b> отменён.'
    else:
        raise ValueError('Неизвестное действие')
    if changed:
        user = await User.select().filter_by(id=order.user_id).first()
        if user:
            try:
                await bot.send_message(user.id, text)
            except Exception:
                pass
    return changed


@router.callback_query(OrderActionCallback.filter())
@transaction(1)
async def order_action(
    callback: CallbackQuery,
    callback_data: OrderActionCallback,
    bot: Bot,
):
    if not _is_admin_chat(callback.message.chat.id):
        return await callback.answer('Недостаточно прав', show_alert=True)
    order = await Order.select().filter_by(id=callback_data.order_id).first()
    if not order:
        return await callback.answer('Заказ не найден', show_alert=True)
    try:
        changed = await _apply_order_action(
            order,
            callback_data.action,
            callback.from_user.id,
            bot,
        )
    except ValueError as exc:
        return await callback.answer(str(exc), show_alert=True)
    await callback.answer('Готово' if changed else 'Уже выполнено')
    if changed:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.message(Command('order'))
@transaction(1)
async def order_command(message: Message, command: CommandObject, bot: Bot):
    if not _is_admin_chat(message.chat.id):
        return await _deny(message)
    parts = (command.args or '').split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer('Формат: <code>/order ID paid</code> или <code>/order ID cancel</code>')
        return
    order = await Order.select().filter_by(id=int(parts[0])).first()
    if not order:
        await message.answer('Заказ не найден.')
        return
    try:
        changed = await _apply_order_action(order, parts[1], message.from_user.id, bot)
    except ValueError as exc:
        await message.answer(html.escape(str(exc)))
        return
    await message.answer('Готово.' if changed else 'Это действие уже выполнено.')


@router.message(Command('discount'))
@transaction(1)
async def discount_command(message: Message, command: CommandObject):
    if not _is_admin_chat(message.chat.id):
        return await _deny(message)
    parts = (command.args or '').rsplit(maxsplit=1)
    if len(parts) != 2:
        await message.answer('Формат: <code>/discount username 5</code>')
        return
    user = await _find_user(parts[0])
    try:
        percent = Decimal(parts[1].replace(',', '.'))
    except InvalidOperation:
        percent = Decimal('-1')
    if not user or not Decimal('0') <= percent <= Decimal('100'):
        await message.answer('Пользователь не найден или процент вне диапазона 0–100.')
        return
    user.personal_discount_percent = percent
    user.updated_at = datetime.now()
    user.add()
    await message.answer(
        f'Персональная скидка пользователя {user.id}: {percent}%.'
    )


@router.message(Command('promo'))
@transaction(1)
async def promo_command(message: Message, command: CommandObject):
    if not _is_admin_chat(message.chat.id):
        return await _deny(message)
    parts = [part.strip() for part in (command.args or '').split('|')]
    if len(parts) != 4:
        await message.answer(
            'Формат: <code>/promo CODE | Имя партнёра | 10 | 10</code>'
        )
        return
    try:
        user_percent = Decimal(parts[2].replace(',', '.'))
        partner_percent = Decimal(parts[3].replace(',', '.'))
    except InvalidOperation:
        await message.answer('Проценты должны быть числами.')
        return
    if not all((parts[0], parts[1])) or not all(
        Decimal('0') <= value <= Decimal('100')
        for value in (user_percent, partner_percent)
    ):
        await message.answer('Проверьте код, имя и диапазон процентов 0–100.')
        return
    code = parts[0].upper()
    promo = await PromoCode.select().where(func.upper(PromoCode.code) == code).first()
    if not promo:
        promo = PromoCode(code=code, partner_name=parts[1]).add()
    promo.partner_name = parts[1]
    promo.user_discount_percent = user_percent
    promo.partner_reward_percent = partner_percent
    promo.is_active = True
    promo.add()
    await message.answer(f'Промокод <b>{html.escape(code)}</b> сохранён и активен.')


async def _toggle_promo(promo: PromoCode):
    promo.is_active = not promo.is_active
    promo.add()


@router.message(Command('promo_toggle'))
@transaction(1)
async def promo_toggle_command(message: Message, command: CommandObject):
    if not _is_admin_chat(message.chat.id):
        return await _deny(message)
    code = (command.args or '').strip().upper()
    promo = await PromoCode.select().where(func.upper(PromoCode.code) == code).first()
    if not promo:
        await message.answer('Промокод не найден.')
        return
    await _toggle_promo(promo)
    await message.answer(f'{promo.code}: {"включён" if promo.is_active else "отключён"}.')


@router.callback_query(PromoToggleCallback.filter())
@transaction(1)
async def promo_toggle_callback(
    callback: CallbackQuery,
    callback_data: PromoToggleCallback,
):
    if not _is_admin_chat(callback.message.chat.id):
        return await callback.answer('Недостаточно прав', show_alert=True)
    promo = await PromoCode.select().filter_by(id=callback_data.promo_id).first()
    if not promo:
        return await callback.answer('Промокод не найден', show_alert=True)
    await _toggle_promo(promo)
    await callback.answer('Включён' if promo.is_active else 'Отключён')
    await callback.message.edit_reply_markup(reply_markup=None)


@plugin.setup()
def include_router(dispatcher: Dispatcher):
    dispatcher.include_router(router)
