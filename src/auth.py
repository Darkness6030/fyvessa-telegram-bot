from datetime import datetime, timedelta, timezone
from typing import Annotated

from aiogram.utils.web_app import safe_parse_webapp_init_data, WebAppInitData
from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from rewire_sqlmodel import transaction

from src.models import User

authorization_header = APIKeyHeader(name='Authorization', auto_error=False)
telegram_init_data_header = APIKeyHeader(
    name='X-Telegram-Init-Data',
    auto_error=False,
)


def parse_telegram_init_data(authorization: str, bot_token: str) -> WebAppInitData:
    try:
        init_data = safe_parse_webapp_init_data(bot_token, authorization)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail='Failed to parse Telegram init data') from exc
    auth_date = init_data.auth_date
    if not auth_date.tzinfo:
        auth_date = auth_date.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if auth_date > now + timedelta(minutes=5) or auth_date < now - timedelta(hours=24):
        raise HTTPException(status_code=401, detail='Telegram session has expired')
    return init_data


async def get_init_data(
    authorization: Annotated[str | None, Depends(authorization_header)],
    telegram_init_data: Annotated[str | None, Depends(telegram_init_data_header)],
) -> WebAppInitData:
    from src.bot import Config
    raw_init_data = telegram_init_data or authorization
    if not raw_init_data:
        raise HTTPException(status_code=401, detail='Откройте магазин из Telegram')
    return parse_telegram_init_data(raw_init_data, Config.token)


@transaction(1)
async def get_init_data_user(init_data: Annotated[WebAppInitData, Depends(get_init_data)]) -> User:
    if not init_data.user:
        raise HTTPException(status_code=401, detail='Telegram user is missing')

    return await User.get_or_create(
        id=init_data.user.id,
        username=init_data.user.username,
        first_name=init_data.user.first_name,
        last_name=init_data.user.last_name,
    )
