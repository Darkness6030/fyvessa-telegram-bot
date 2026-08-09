from aiogram import Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
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
router = Router(name="main-flow")


def _main_keyboard() -> InlineKeyboardMarkup:
    support_username = Config.support_username.lstrip("@")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛍 Каталог",
                    web_app=WebAppInfo(url=Config.mini_app_url),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Отзывы",
                    web_app=WebAppInfo(
                        url=f"{Config.mini_app_url.rstrip('/')}/reviews"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬 Поддержка",
                    url=f"https://t.me/{support_username}",
                ),
                InlineKeyboardButton(text="📣 Канал", url=Config.channel_url),
            ],
        ],
    )


@plugin.setup()
def include_main_router(dispatcher: Dispatcher) -> None:
    dispatcher.include_router(router)


@router.message(CommandStart())
@transaction(1)
async def start(message: Message) -> None:
    if message.from_user is None:
        return

    await Customer.get_or_create(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    await message.answer(
        "<b>Добро пожаловать в Fyvessa!</b> 👋\n\n"
        "Здесь можно найти нужный товар, посмотреть новинки и популярное, "
        "использовать скидки и коины и оформить заказ прямо в Telegram.\n\n"
        "Для начала откройте каталог. Если понадобится помощь — поддержка рядом.",
        reply_markup=_main_keyboard(),
    )


@router.message(Command("catalog"))
async def catalog(message: Message) -> None:
    await message.answer(
        "Каталог откроется внутри Telegram:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть каталог",
                        web_app=WebAppInfo(url=Config.mini_app_url),
                    ),
                ],
            ]
        ),
    )
