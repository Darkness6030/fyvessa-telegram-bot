from aiogram import Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import (
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from pydantic import BaseModel
from rewire import config, simple_plugin
from rewire_sqlmodel import transaction

from src.keyboards import inline_keyboard
from src.models import User


@config
class Config(BaseModel):
    mini_app_url: str
    channel_url: str
    reviews_channel_url: str
    support_username: str


plugin = simple_plugin()
router = Router(name='main')


def create_main_keyboard() -> InlineKeyboardMarkup:
    support_username = Config.support_username.lstrip('@')
    return inline_keyboard([
        ('🛍 Каталог', WebAppInfo(url=Config.mini_app_url)),
        ('⭐ Отзывы', Config.reviews_channel_url or Config.channel_url),
        ('💬 Поддержка', f'https://t.me/{support_username}'),
        ('📣 Канал', Config.channel_url),
    ])


@router.message(CommandStart())
@transaction(1)
async def start(message: Message, command: CommandObject):
    referrer_id = None
    if command.args and command.args.isdigit():
        referrer = await User.get_by_id(int(command.args))
        if referrer:
            referrer_id = referrer.id

    await User.get_or_create(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        referrer_id=referrer_id,
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
        reply_markup=inline_keyboard([
            ('Открыть каталог', WebAppInfo(url=Config.mini_app_url)),
        ]),
    )


@plugin.setup()
def include_router(dispatcher: Dispatcher):
    dispatcher.include_router(router)
