import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

import gspread
from gspread.utils import ValueRenderOption
from pydantic import BaseModel, ConfigDict
from rewire import logger, simple_plugin

from src.sheet_utils import (
    CREDENTIALS_PATH,
    SETTINGS,
    SETTINGS_SHEET,
    SPREADSHEET_TITLE,
    CellUpdate,
    columns,
    get_worksheet,
    has_values,
    header_key,
    raw_row,
    row_updates,
    write_updates,
)

plugin = simple_plugin()

SETTING_DESCRIPTIONS = {
    'channel_url': 'Ссылка на основной Telegram-канал',
    'reviews_channel_url': 'Ссылка на Telegram-канал с отзывами',
    'support_url': 'Ссылка на поддержку или @username',
    'payment_details': 'Реквизиты для оплаты (можно в несколько строк)',
}

SETTING_ALIASES = {
    'основной канал': 'channel_url',
    'канал': 'channel_url',
    'отзывы': 'reviews_channel_url',
    'канал отзывов': 'reviews_channel_url',
    'поддержка': 'support_url',
    'support_username': 'support_url',
    'реквизиты': 'payment_details',
    'реквизиты для оплаты': 'payment_details',
}


class SettingsValidationError(ValueError):
    pass


class StoreSettings(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    channel_url: str = ''
    reviews_channel_url: str = ''
    support_url: str = ''
    payment_details: str = ''


_settings = StoreSettings()


def get_settings() -> StoreSettings:
    return _settings


def _normalize_link(value: Any) -> str:
    link = str(value or '').strip()
    if not link:
        return ''

    if link.startswith('@'):
        link = f'https://t.me/{link[1:]}'
    elif re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', link):
        link = f'https://t.me/{link}'
    elif link.casefold().startswith(('t.me/', 'telegram.me/', 'www.')):
        link = f'https://{link}'

    parsed = urlsplit(link)
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.hostname
        or re.search(r'\s', link)
    ):
        return ''
    return link.rstrip('/')


def _setting_key(value: Any) -> str:
    canonical, alias = header_key(value)
    return SETTING_ALIASES.get(alias, canonical)


def _normalize_settings(
    values: list[list[Any]],
) -> tuple[StoreSettings, list[CellUpdate]]:
    column_map, updates = columns(values, SETTINGS)
    setting_values: dict[str, str] = {}

    for row_number, values_row in enumerate(values[1:], start=2):
        if not has_values(values_row):
            continue

        raw = raw_row(values_row, column_map)
        key = _setting_key(raw['key'])
        if key not in SETTING_DESCRIPTIONS or key in setting_values:
            continue

        value = str(raw['value'] or '').strip()
        if key.endswith('_url'):
            value = _normalize_link(value)

        normalized = {
            'key': key,
            'value': value,
            'description': SETTING_DESCRIPTIONS[key],
        }
        setting_values[key] = value
        updates.extend(row_updates(row_number, raw, normalized, column_map))

    next_row = max(len(values) + 1, 2)
    for key, description in SETTING_DESCRIPTIONS.items():
        if key in setting_values:
            continue
        setting_values[key] = ''
        updates.extend(
            CellUpdate(next_row, column_map[field], value)
            for field, value in (
                ('key', key),
                ('value', ''),
                ('description', description),
            )
        )
        next_row += 1

    return StoreSettings.model_validate(setting_values), updates


def _load_store_settings() -> StoreSettings:
    if not CREDENTIALS_PATH.is_file():
        raise SettingsValidationError(
            f'Google credentials file not found: {CREDENTIALS_PATH}',
        )

    try:
        client = gspread.service_account(filename=str(CREDENTIALS_PATH))
        spreadsheet = client.open(SPREADSHEET_TITLE)
        worksheet = get_worksheet(spreadsheet, SETTINGS_SHEET, SETTINGS)
        values = worksheet.get_all_values(
            value_render_option=ValueRenderOption.unformatted,
        )
        settings, updates = _normalize_settings(values)
        write_updates(worksheet, updates)
        return settings
    except gspread.SpreadsheetNotFound as exc:
        raise SettingsValidationError(
            f'Google spreadsheet {SPREADSHEET_TITLE!r} was not found or not shared '
            'with the service account',
        ) from exc
    except SettingsValidationError:
        raise
    except Exception as exc:
        raise SettingsValidationError(
            f'Google Sheets settings could not be loaded: {exc}',
        ) from exc


async def sync_settings() -> StoreSettings:
    global _settings
    _settings = await asyncio.to_thread(_load_store_settings)
    return _settings


@plugin.setup()
async def import_settings() -> None:
    try:
        await sync_settings()
    except SettingsValidationError as exc:
        logger.error('Settings setup sync failed: {}', exc)
