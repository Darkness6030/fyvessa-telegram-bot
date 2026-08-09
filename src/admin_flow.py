import html
from typing import Optional

from aiogram import Dispatcher, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pydantic import BaseModel
from rewire import config, simple_plugin
from rewire_sqlmodel import transaction

from src.catalog import CatalogValidationError, sync_catalog
from src.models import AvailabilityRequest, Customer, Order, Product


@config
class Config(BaseModel):
    admin_ids: str = ""
    products_path: str = "assets/products.xlsx"


plugin = simple_plugin()
router = Router(name="admin-flow")


class SyncProductsCallback(CallbackData, prefix="admin_sync_products"):
    pass


class AdminSummaryCallback(CallbackData, prefix="admin_summary"):
    pass


def _is_admin(user_id: Optional[int]) -> bool:
    admin_ids = {
        int(value.strip())
        for value in Config.admin_ids.split(",")
        if value.strip()
    }
    return user_id is not None and user_id in admin_ids


async def _deny(message: Message) -> None:
    await message.answer("Команда доступна только администратору.")


@plugin.setup()
def include_admin_router(dispatcher: Dispatcher) -> None:
    dispatcher.include_router(router)


@router.message(Command("admin"))
async def admin_menu(message: Message) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None):
        await _deny(message)
        return
    await message.answer(
        "<b>Администрирование Fyvessa</b>\n\n"
        "Каталог редактируется только в Excel. После сохранения файла запустите синхронизацию.",
        reply_markup=InlineKeyboardBuilder()
        .button(
            text="🔄 Синхронизировать Excel",
            callback_data=SyncProductsCallback(),
        )
        .button(text="📊 Сводка", callback_data=AdminSummaryCallback())
        .adjust(1)
        .as_markup(),
    )


async def _run_sync(message: Message) -> None:
    try:
        report = await sync_catalog(Config.products_path)
    except CatalogValidationError as exc:
        await message.answer(
            f"❌ <b>Excel не импортирован</b>\n<pre>{html.escape(str(exc))}</pre>"
        )
        return
    await message.answer(
        "✅ <b>Каталог синхронизирован</b>\n\n"
        f"Создано товаров: {report.created}\n"
        f"Обновлено товаров: {report.updated}\n"
        f"Скрыто товаров: {report.hidden}\n"
        f"Создано категорий: {report.categories_created}"
    )


@router.message(Command("sync_products"))
async def sync_products_command(message: Message) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None):
        await _deny(message)
        return
    await _run_sync(message)


@router.callback_query(SyncProductsCallback.filter())
async def sync_products_callback(
    callback: CallbackQuery,
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await _run_sync(callback.message)


@router.callback_query(AdminSummaryCallback.filter())
@transaction(1)
async def summary_callback(
    callback: CallbackQuery,
) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer()

    products = await Product.select().all()
    customers = await Customer.select().all()
    orders = await Order.select().all()
    pending = await AvailabilityRequest.select().filter_by(status="pending").all()
    text = (
        "<b>Текущая сводка</b>\n\n"
        f"Активных товаров: {sum(product.is_active for product in products)}\n"
        f"Клиентов: {len(customers)}\n"
        f"Заказов: {len(orders)}\n"
        f"Запросов наличия без ответа: {len(pending)}"
    )
    if isinstance(callback.message, Message):
        await callback.message.answer(text)
