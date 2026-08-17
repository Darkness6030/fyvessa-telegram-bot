import hashlib
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Generic, Optional, TypeVar

import gspread
from gspread.exceptions import APIError
from gspread.utils import ValidationConditionType, ValueInputOption, ValueRenderOption

SPREADSHEET_TITLE = 'Fyvessa Admin'
CREDENTIALS_PATH = Path('assets/credentials.json')
MONEY_QUANT = Decimal('0.01')
MAX_MONEY = Decimal('9999999999.99')


@dataclass(frozen=True)
class SheetSpec:
    columns: tuple[str, ...]
    aliases: dict[str, str]
    display_names: dict[str, str] | None = None
    checkbox_fields: tuple[str, ...] = ()


PRODUCTS = SheetSpec(
    columns=(
        'image_url', 'sku', 'name', 'description', 'characteristics',
        'retail_price', 'wholesale_price', 'discount_price',
        'is_active', 'is_popular', 'is_new', 'owner',
    ),
    aliases={
        'артикул': 'sku',
        'название': 'name',
        'описание': 'description',
        'характеристики': 'characteristics',
        'розничная цена': 'retail_price',
        'закупочная цена': 'wholesale_price',
        'оптовая цена': 'wholesale_price',
        'цена со скидкой': 'discount_price',
        'изображение': 'image_url',
        'фото': 'image_url',
        'фото товара': 'image_url',
        'активен': 'is_active',
        'популярный': 'is_popular',
        'новинка': 'is_new',
        'рекомендуемый': 'is_new',
        'is recommended': 'is_new',
        'владелец': 'owner',
    },
    checkbox_fields=('is_active', 'is_popular', 'is_new'),
)

OWNERS = SheetSpec(
    columns=('name', 'share_percent'),
    aliases={
        'владелец': 'name',
        'имя': 'name',
        'доля': 'share_percent',
        'процент': 'share_percent',
        'доля владельца': 'share_percent',
    },
    display_names={
        'name': 'Владелец',
        'share_percent': 'Доля',
    },
)

SETTINGS = SheetSpec(
    columns=('key', 'value', 'description'),
    aliases={
        'ключ': 'key',
        'значение': 'value',
        'описание': 'description',
    },
    display_names={
        'key': 'Ключ',
        'value': 'Значение',
        'description': 'Описание',
    },
)

OWNERS_SHEET = 'Владельцы'
SETTINGS_SHEET = 'Настройки'
RESERVED_SHEET_TITLES = frozenset({
    OWNERS_SHEET.casefold(),
    SETTINGS_SHEET.casefold(),
})

T = TypeVar('T')


@dataclass(frozen=True)
class CellUpdate:
    row: int
    column: int
    value: Any


@dataclass
class Normalized(Generic[T]):
    rows: list[T]
    updates: list[CellUpdate]

    @property
    def corrected_rows(self) -> int:
        return len({update.row for update in self.updates if update.row > 1})


def columns(
    values: list[list[Any]],
    spec: SheetSpec,
) -> tuple[dict[str, int], list[CellUpdate]]:
    headers = list(values[0]) if values else []
    result: dict[str, int] = {}
    updates = []

    for column, value in enumerate(headers, start=1):
        canonical, alias = header_key(value)
        field = spec.aliases.get(alias, canonical)
        if field not in spec.columns or field in result:
            continue
        result[field] = column
        expected_header = (spec.display_names or {}).get(field, field)
        if str(value or '').strip() != expected_header:
            updates.append(CellUpdate(1, column, expected_header))

    for field in spec.columns:
        if field in result:
            continue
        result[field] = len(headers) + 1
        header = (spec.display_names or {}).get(field, field)
        headers.append(header)
        updates.append(CellUpdate(1, len(headers), header))

    return result, updates


def header_key(value: Any) -> tuple[str, str]:
    text = str(value or '').strip().casefold()
    alias = ' '.join(text.replace('_', ' ').replace('-', ' ').split())
    canonical = re.sub(r'[^a-z0-9_]+', '_', text.replace('-', '_')).strip('_')
    return canonical, alias


def raw_row(values: list[Any], column_map: dict[str, int]) -> dict[str, Any]:
    return {
        field: values[column - 1] if column <= len(values) else ''
        for field, column in column_map.items()
    }


def has_values(row: list[Any]) -> bool:
    return any(
        value is not None
        and not isinstance(value, bool)
        and str(value).strip()
        for value in row
    )


def has_records(values: list[list[Any]]) -> bool:
    return bool(values) and any(has_values(row) for row in values[1:])


def as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)

    normalized = str(value or '').strip().casefold()
    if not normalized:
        return default
    if normalized in {'1', 'true', 'yes', 'y', 'да', 'активен', 'вкл', '+'}:
        return True
    if normalized in {'0', 'false', 'no', 'n', 'нет', 'скрыт', 'выкл', '-'}:
        return False
    return default


def row_updates(
    row_number: int,
    raw: dict[str, Any],
    normalized: dict[str, Any],
    column_map: dict[str, int],
) -> list[CellUpdate]:
    return [
        CellUpdate(row_number, column_map[field], sheet_value(field, value))
        for field, value in normalized.items()
        if _changed(raw.get(field), sheet_value(field, value))
    ]


def unique(value: str, seen: set[str]) -> str:
    candidate = value
    suffix = 2
    while candidate.casefold() in seen:
        candidate = f'{value} {suffix}'
        suffix += 1
    seen.add(candidate.casefold())
    return candidate


def get_worksheet(
    spreadsheet: gspread.Spreadsheet,
    title: str,
    spec: SheetSpec,
) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(
            title=title,
            rows=1000,
            cols=len(spec.columns),
        )


def worksheet_values(
    spreadsheet: gspread.Spreadsheet,
    worksheets: list[gspread.Worksheet],
) -> dict[str, list[list[Any]]]:
    ranges = [f"'{worksheet.title.replace(chr(39), chr(39) * 2)}'" for worksheet in worksheets]
    response = spreadsheet.values_batch_get(
        ranges,
        params={'valueRenderOption': ValueRenderOption.unformatted.value},
    )

    value_ranges = response.get('valueRanges', [])
    return {
        worksheet.title: (
            value_ranges[index].get('values', [])
            if index < len(value_ranges)
            else []
        )
        for index, worksheet in enumerate(worksheets)
    }


def write_updates(worksheet: gspread.Worksheet, updates: list[CellUpdate]) -> None:
    if not updates:
        return

    required_columns = max(update.column for update in updates)
    if required_columns > worksheet.col_count:
        worksheet.add_cols(required_columns - worksheet.col_count)

    required_rows = max(update.row for update in updates)
    if required_rows > worksheet.row_count:
        worksheet.add_rows(required_rows - worksheet.row_count)

    worksheet.batch_update(
        [{
            'range': gspread.utils.rowcol_to_a1(update.row, update.column),
            'values': [[update.value]],
        } for update in updates],
        value_input_option=ValueInputOption.raw,
    )


def ensure_checkboxes(
    spreadsheet: gspread.Spreadsheet,
    worksheets: list[tuple[gspread.Worksheet, dict[str, int]]],
    spec: SheetSpec,
) -> None:
    for worksheet, column_map in worksheets:
        requests = []
        for field in spec.checkbox_fields:
            column_index = column_map[field] - 1
            requests.append({
                'setDataValidation': {
                    'range': {
                        'sheetId': worksheet.id,
                        'startRowIndex': 1,
                        'endRowIndex': worksheet.row_count,
                        'startColumnIndex': column_index,
                        'endColumnIndex': column_index + 1,
                    },
                    'rule': {
                        'condition': {'type': ValidationConditionType.boolean.value},
                        'strict': True,
                    },
                },
            })
        try:
            spreadsheet.batch_update({'requests': requests})
        except APIError as exc:
            # Google Sheets tables already render typed boolean columns as
            # checkboxes and reject an additional validation rule.
            if 'not allowed on cells in typed columns' not in str(exc):
                raise


def as_decimal(value: Any) -> Optional[Decimal]:
    normalized = str(value or '').strip().casefold()
    for token in ('\u00a0', ' ', '₽', 'рублей', 'рубля', 'руб.', 'руб', '%'):
        normalized = normalized.replace(token, '')
    if not normalized:
        return None

    try:
        result = Decimal(normalized.replace(',', '.'))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def as_money(value: Any) -> Optional[Decimal]:
    result = as_decimal(value)
    if result is None or abs(result) > MAX_MONEY:
        return result

    try:
        return result.quantize(MONEY_QUANT)
    except InvalidOperation:
        return None


def normalize_sku(value: Any, name: str, category: str) -> str:
    original = str(value or '').strip()
    normalized = unicodedata.normalize('NFKD', original)
    normalized = normalized.encode('ascii', 'ignore').decode().upper()
    normalized = re.sub(r'[^A-Z0-9._-]+', '-', normalized).strip('-._')
    if original.isascii() and normalized:
        return normalized[:100]

    digest = hashlib.sha1(f'{name}\0{category}'.encode()).hexdigest()[:10].upper()
    return f'ITEM-{digest}'


def sheet_value(field: str, value: Any) -> Any:
    if field in {'retail_price', 'wholesale_price', 'discount_price', 'share_percent'}:
        if value is None:
            return ''
        return int(value) if value == value.to_integral_value() else float(value)
    if field in {'is_active', 'is_popular', 'is_new'}:
        return bool(value)
    return '' if value is None else str(value)


def _changed(raw: Any, normalized: Any) -> bool:
    if isinstance(normalized, bool):
        return not isinstance(raw, bool) or raw is not normalized
    raw_text = '' if raw is None else str(raw)
    return raw_text.strip() != str(normalized).strip()
