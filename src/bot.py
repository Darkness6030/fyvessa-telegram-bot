from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.callback_answer import CallbackAnswerMiddleware
from pydantic import BaseModel
from rewire import config, simple_plugin


@config
class Config(BaseModel):
    token: str


plugin = simple_plugin()


@plugin.setup()
async def create_bot() -> Bot:
    return Bot(
        token=Config.token,
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
