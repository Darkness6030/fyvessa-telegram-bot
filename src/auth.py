from typing import Annotated

from aiogram.utils.web_app import safe_parse_webapp_init_data, WebAppInitData
from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from rewire_sqlmodel import transaction

from src.models import Customer

authorization_header = APIKeyHeader(name='Authorization')


def parse_telegram_init_data(authorization: str, bot_token: str) -> WebAppInitData:
    try:
        return safe_parse_webapp_init_data(bot_token, authorization)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail='Failed to parse Telegram init data') from exc


async def get_init_data(authorization: Annotated[str, Depends(authorization_header)]) -> WebAppInitData:
    from src.bot import Config
    return parse_telegram_init_data(authorization, Config.token)


@transaction(1)
async def get_init_data_user(init_data: Annotated[WebAppInitData, Depends(get_init_data)]) -> Customer:
    if not init_data.user:
        raise HTTPException(status_code=401, detail='Telegram user is missing')

    return await Customer.get_or_create(
        user_id=init_data.user.id,
        username=init_data.user.username,
        first_name=init_data.user.first_name,
        last_name=init_data.user.last_name,
    )
