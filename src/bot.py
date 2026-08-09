from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.callback_answer import CallbackAnswerMiddleware
from pydantic import BaseModel
from rewire import config, simple_plugin


@config
class Config(BaseModel):
    token: str
    api_url: Optional[str] = None


plugin = simple_plugin()


@plugin.setup()
async def create_bot() -> Bot:
    session = AiohttpSession(api=TelegramAPIServer.from_base(Config.api_url), limit=1024) \
        if Config.api_url \
        else AiohttpSession(limit=1024)

    return Bot(
        token=Config.token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


@plugin.setup()
async def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.callback_query.middleware(CallbackAnswerMiddleware())
    return dispatcher


@plugin.run()
async def run_bot(bot: Bot, dispatcher: Dispatcher):
    await dispatcher.start_polling(bot)
