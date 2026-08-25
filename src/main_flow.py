import html

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
from src.models import SocialChannel, User
from src.referrals import award_referral_activation, initialize_referral_rewards
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
    '✨ <b>ДОБРО ПОЖАЛОВАТЬ В FYVESSA</b> ✨\n\n'
    'Мы рады приветствовать вас в нашем магазине 🖤\n\n'
    'Здесь вы найдёте всё, чтобы подчеркнуть свой стиль и создать особенное '
    'настроение:\n'
    '🖤 парфюмерию\n'
    '✨ красоту и уход\n'
    '🎧 технику\n'
    '💎 аксессуары\n\n'
    'Мы тщательно подбираем ассортимент и заботимся о качестве каждого заказа.\n\n'
    'А чтобы вы могли познакомиться с мнением наших покупателей, мы собрали '
    'отзывы наших клиентов в отдельном канале:\n\n'
    '💬 <b>ОТЗЫВЫ</b>\n'
    '{reviews_link}\n\n'
    'Спасибо, что выбираете FYVESSA 🖤\n'
    'Ваш стиль — наша эстетика.'
)


def create_welcome_text() -> str:
    reviews_url = html.escape(get_settings().reviews_channel_url, quote=True)
    reviews_link = (
        f'<a href="{reviews_url}">Перейти к отзывам</a>'
        if reviews_url
        else 'Перейти к отзывам'
    )
    return WELCOME_TEXT.format(reviews_link=reviews_link)


def create_main_keyboard() -> InlineKeyboardMarkup:
    settings = get_settings()
    buttons = [
        ('🛒 Каталог', WebAppInfo(url=f'{Config.mini_app_url.rstrip('/')}')),
    ]

    buttons.extend([
        ('⭐ Отзывы', settings.reviews_channel_url),
        ('💬 Поддержка', settings.support_url),
        ('📣 Канал', settings.channel_url),
    ])

    return inline_keyboard(buttons)


async def _main_keyboard_for_user(user: User) -> InlineKeyboardMarkup:
    channels = await SocialChannel.get_active()
    return create_main_keyboard(channels)


@router.message(CommandStart())
@transaction(1)
async def start(message: Message, command: CommandObject):
    existing_user = await User.get_by_id(message.from_user.id)
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

    if existing_user is None:
        await award_referral_activation(user)
    await initialize_referral_rewards(user)
    await message.answer(
        create_welcome_text(),
        reply_markup=await _main_keyboard_for_user(user),
    )


@router.callback_query(StartCallback.filter(F.action == 'continue'))
@transaction(1)
async def continue_to_start(callback: CallbackQuery):
    await callback.message.edit_text(
        create_welcome_text(),
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
