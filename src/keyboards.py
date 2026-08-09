from typing import Any

from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder


def inline_keyboard(buttons: list[tuple[str, Any]], *layout: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, action in buttons:
        if isinstance(action, WebAppInfo):
            builder.button(text=text, web_app=action)
        elif isinstance(action, str):
            builder.button(text=text, url=action)
        else:
            builder.button(text=text, callback_data=action)

    return builder.adjust(*(layout or (1,))).as_markup()
