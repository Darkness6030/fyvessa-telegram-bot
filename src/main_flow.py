from aiogram import Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pydantic import BaseModel
from rewire import config, simple_plugin
from rewire_sqlmodel import transaction

from src.models import Customer


@config
class Config(BaseModel):
    mini_app_url: str
    channel_url: str
    support_username: str


plugin = simple_plugin()
router = Router(name='main')


def create_main_keyboard() -> InlineKeyboardMarkup:
    support_username = Config.support_username.lstrip('@')
    return (
        InlineKeyboardBuilder()
        .button(text='🛍 Каталог', web_app=WebAppInfo(url=Config.mini_app_url))
        .button(text='⭐ Отзывы', web_app=WebAppInfo(url=f'{Config.mini_app_url.rstrip('/')}/reviews'))
        .button(text='💬 Поддержка', url=f'https://t.me/{support_username}')
        .button(text='📣 Канал', url=Config.channel_url)
        .adjust(1).as_markup()
    )


@router.message(CommandStart())
@transaction(1)
async def start(message: Message):
    await Customer.get_or_create(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    await message.answer(
        '<b>Добро пожаловать в Fyvessa!</b> 👋\n\n'
        'Здесь можно найти нужный товар, посмотреть новинки и популярное, '
        'использовать скидки и коины и оформить заказ прямо в Telegram.\n\n'
        'Для начала откройте каталог. Если понадобится помощь — поддержка рядом.',
        reply_markup=create_main_keyboard(),
    )


@router.message(Command('catalog'))
async def catalog(message: Message):
    await message.answer(
        'Каталог откроется внутри Telegram:',
        reply_markup=InlineKeyboardBuilder()
        .button(text='Открыть каталог', web_app=WebAppInfo(url=Config.mini_app_url))
        .adjust(1).as_markup()
    )


@plugin.setup()
def include_router(dispatcher: Dispatcher):
    dispatcher.include_router(router)
