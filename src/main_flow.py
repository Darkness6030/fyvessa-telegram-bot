from aiogram import Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import (
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pydantic import BaseModel, field_validator
from rewire import config, simple_plugin
from rewire_sqlmodel import transaction

from src.models import User


@config
class Config(BaseModel):
    mini_app_url: str
    channel_url: str
    reviews_channel_url: str
    support_username: str

    @field_validator('mini_app_url')
    @classmethod
    def validate_mini_app_url(cls, value: str) -> str:
        value = value.strip().rstrip('/')
        if not value.startswith('https://'):
            raise ValueError('MINI_APP_URL должен быть публичным HTTPS-адресом')
        if value in {'https://example.com', 'https://www.example.com'}:
            raise ValueError('MINI_APP_URL всё ещё указывает на тестовый example.com')
        return value


plugin = simple_plugin()
router = Router(name='main')
MINI_APP_VERSION = '20260809-3'


def mini_app_url() -> str:
    separator = '&' if '?' in Config.mini_app_url else '?'
    return f'{Config.mini_app_url}{separator}app_version={MINI_APP_VERSION}'


def create_main_keyboard() -> InlineKeyboardMarkup:
    support_username = Config.support_username.lstrip('@')
    return (
        InlineKeyboardBuilder()
        .button(text='🛍 Каталог', web_app=WebAppInfo(url=mini_app_url()))
        .button(text='⭐ Отзывы', url=Config.reviews_channel_url or Config.channel_url)
        .button(text='💬 Поддержка', url=f'https://t.me/{support_username}')
        .button(text='📣 Канал', url=Config.channel_url)
        .adjust(1).as_markup()
    )


@router.message(CommandStart())
@transaction(1)
async def start(message: Message, command: CommandObject):
    user = await User.get_or_create(
        id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    if not user.referrer_id and (command.args or '').isdigit():
        referrer_id = int(command.args)
        if referrer_id != user.id and await User.select().filter_by(id=referrer_id).first():
            user.referrer_id = referrer_id
            user.add()

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
        .button(text='Открыть каталог', web_app=WebAppInfo(url=mini_app_url()))
        .adjust(1).as_markup()
    )


@plugin.setup()
def include_router(dispatcher: Dispatcher):
    dispatcher.include_router(router)
