from aiogram import Dispatcher, F, Router
from aiogram.enums import ChatType
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
from src.referrals import initialize_referral_rewards
from src.settings import get_settings


@config
class Config(BaseModel):
    mini_app_url: str


plugin = simple_plugin()
router = Router(name='main')

router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


def create_main_keyboard() -> InlineKeyboardMarkup:
    settings = get_settings()
    return inline_keyboard([
        ('СТАРТ', WebAppInfo(url=f'{Config.mini_app_url.rstrip('/')}/')),
        ('⭐ Отзывы', settings.reviews_channel_url),
        ('💬 Поддержка', settings.support_url),
        ('📣 Канал', settings.channel_url),
    ])


@router.message(CommandStart())
@transaction(1)
async def start(message: Message, command: CommandObject):
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

    await message.answer(
        '<b>Добро пожаловать в Fyvessa!</b> 👋\n\n'
        'Здесь можно найти нужный товар, посмотреть новинки и популярное, '
        'использовать скидки и коины и оформить заказ прямо в Telegram.\n\n'
        'Нажмите «СТАРТ», чтобы открыть магазин. Если понадобится помощь — '
        'поддержка рядом.',
        reply_markup=create_main_keyboard(),
    )


@router.message(Command('catalog'))
async def catalog(message: Message):
    await message.answer(
        'Каталог откроется внутри Telegram:',
        reply_markup=inline_keyboard([
            ('СТАРТ', WebAppInfo(url=f'{Config.mini_app_url.rstrip('/')}/catalog')),
        ]),
    )


@plugin.setup()
def include_router(dispatcher: Dispatcher):
    dispatcher.include_router(router)
