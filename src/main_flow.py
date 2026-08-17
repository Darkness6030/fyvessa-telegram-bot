from aiogram import Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.filters.command import CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from pydantic import BaseModel
from rewire import config, simple_plugin
from rewire_sqlmodel import transaction

from src.keyboards import inline_keyboard
from src.models import User
from src.referrals import initialize_referral_rewards
from src.settings import get_settings


@config
class Config(BaseModel):
    mini_app_url: str


plugin = simple_plugin()
router = Router(name='main')

router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class StartCallback(CallbackData, prefix='start'):
    action: str


WELCOME_TEXT = (
    '<b>Добро пожаловать в Fyvessa!</b> 👋\n\n'
    'Рады видеть вас в нашем магазине. Нажмите «СТАРТ», чтобы продолжить.'
)

START_TEXT = (
    '<b>Добро пожаловать в Fyvessa!</b> 👋\n\n'
    'Здесь можно найти нужный товар, посмотреть новинки и популярное, '
    'использовать скидки и коины и оформить заказ прямо в Telegram.\n\n'
    'Если понадобится помощь — поддержка рядом.'
)


def create_welcome_keyboard() -> InlineKeyboardMarkup:
    return inline_keyboard([
        ('СТАРТ', StartCallback(action='continue')),
    ])


def create_main_keyboard() -> InlineKeyboardMarkup:
    settings = get_settings()
    return inline_keyboard([
        ('🛒 Каталог', WebAppInfo(url=f'{Config.mini_app_url.rstrip('/')}/catalog')),
        ('⭐ Отзывы', settings.reviews_channel_url),
        ('💬 Поддержка', settings.support_url),
        ('📣 Канал', settings.channel_url),
    ])


@router.message(CommandStart())
@transaction(1)
async def start(message: Message, command: CommandObject):
    is_first_start = await User.get_by_id(message.from_user.id) is None
    referrer_id = None
    if command.args and command.args.isdigit():
        referrer = await User.get_by_id(int(command.args))
        if referrer:
            referrer_id = referrer.id

    user = await User.get_or_create(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        referrer_id=referrer_id,
    )
    await initialize_referral_rewards(user)

    if is_first_start:
        return await message.answer(
            WELCOME_TEXT,
            reply_markup=create_welcome_keyboard(),
        )

    await message.answer(
        START_TEXT,
        reply_markup=create_main_keyboard(),
    )


@router.callback_query(StartCallback.filter(F.action == 'continue'))
async def continue_to_start(callback: CallbackQuery):
    await callback.message.edit_text(
        START_TEXT,
        reply_markup=create_main_keyboard(),
    )
    await callback.answer()


@router.message(Command('catalog'))
async def catalog(message: Message):
    await message.answer(
        'Каталог откроется внутри Telegram:',
        reply_markup=inline_keyboard([
            ('🛒 Каталог', WebAppInfo(url=f'{Config.mini_app_url.rstrip('/')}/catalog')),
        ]),
    )


@plugin.setup()
def include_router(dispatcher: Dispatcher):
    dispatcher.include_router(router)
