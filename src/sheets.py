import asyncio
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Generic, Optional, TypeVar
from urllib.parse import urlsplit

import gspread
from gspread.utils import (a1_range_to_grid_range, ValidationConditionType, ValueInputOption, ValueRenderOption)

from src.catalog import (
    CatalogRow,
    CatalogSource,
    CatalogValidationError,
    CategoryRow,
    OwnerRow,
)
from src.settings import SettingsValidationError, StoreSettings
from src.sheet_images import load_manifest
from src.sheet_schema import (as_decimal, as_money, MAX_MONEY, normalize_sku, OWNERS, PRODUCTS, SETTINGS, sheet_value, SheetSpec)

SPREADSHEET_TITLE = 'Fyvessa Admin'
CREDENTIALS_PATH = Path('assets/credentials.json')
CONTROL_SHEET_TITLES = frozenset(
    {PRODUCTS.title, 'categories', OWNERS.title, SETTINGS.title}
)

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


def _header_key(value: Any) -> tuple[str, str]:
    text = str(value or '').strip().casefold()
    alias = ' '.join(text.replace('_', ' ').replace('-', ' ').split())
    canonical = re.sub(r'[^a-z0-9_]+', '_', text.replace('-', '_')).strip('_')
    return canonical, alias


def _columns(values: list[list[Any]], spec: SheetSpec) -> tuple[dict[str, int], list[CellUpdate]]:
    headers = list(values[0]) if values else []
    result = {}

    updates = []
    for column, value in enumerate(headers, start=1):
        canonical, alias = _header_key(value)
        field = spec.aliases.get(alias, canonical)
        if field in spec.columns and field not in result:
            result[field] = column
            if str(value or '').strip() != field:
                updates.append(CellUpdate(1, column, field))

    for field in spec.columns:
        if field not in result:
            result[field] = len(headers) + 1
            headers.append(field)
            updates.append(CellUpdate(1, len(headers), field))

    return result, updates


def _raw_row(values: list[Any], columns: dict[str, int]) -> dict[str, Any]:
    return {
        field: values[column - 1] if column <= len(values) else ''
        for field, column in columns.items()
    }


def _has_values(row: list[Any]) -> bool:
    return any(
        value is not None
        and not isinstance(value, bool)
        and str(value).strip()
        for value in row
    )


def _has_records(values: list[list[Any]]) -> bool:
    return bool(values) and any(_has_values(row) for row in values[1:])


def _as_bool(value: Any, default: bool) -> bool:
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


def _changed(raw: Any, normalized: Any) -> bool:
    if isinstance(normalized, bool):
        return not isinstance(raw, bool) or raw is not normalized

    raw_text = '' if raw is None else str(raw)
    return raw_text.strip() != str(normalized).strip()


def _row_updates(
    row_number: int,
    raw: dict[str, Any],
    normalized: dict[str, Any],
    columns: dict[str, int],
) -> list[CellUpdate]:
    return [
        CellUpdate(row_number, columns[field], sheet_value(field, value))
        for field, value in normalized.items()
        if _changed(raw.get(field), sheet_value(field, value))
    ]


def _unique(value: str, seen: set[str]) -> str:
    candidate = value
    suffix = 2
    while candidate.casefold() in seen:
        candidate = f'{value} {suffix}'
        suffix += 1

    seen.add(candidate.casefold())
    return candidate


def _setting_key(value: Any) -> str:
    canonical, alias = _header_key(value)
    return SETTING_ALIASES.get(alias, canonical)


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


def _image_reference(value: Any) -> Optional[str]:
    reference = str(value or '').strip()
    if not reference:
        return None
    if reference.startswith('/'):
        return reference

    parsed = urlsplit(reference)
    if parsed.scheme in {'http', 'https'} and parsed.hostname:
        return reference
    return None


def _normalize_settings(
    values: list[list[Any]],
) -> tuple[StoreSettings, list[CellUpdate]]:
    columns, updates = _columns(values, SETTINGS)
    setting_values: dict[str, str] = {}

    for row_number, values_row in enumerate(values[1:], start=2):
        if not _has_values(values_row):
            continue

        raw = _raw_row(values_row, columns)
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
        updates.extend(_row_updates(row_number, raw, normalized, columns))

    next_row = max(len(values) + 1, 2)
    for key, description in SETTING_DESCRIPTIONS.items():
        if key in setting_values:
            continue

        setting_values[key] = ''
        updates.extend(
            CellUpdate(next_row, columns[field], value)
            for field, value in (
                ('key', key),
                ('value', ''),
                ('description', description),
            )
        )
        next_row += 1

    return StoreSettings.model_validate(setting_values), updates


def _normalize_owners(values: list[list[Any]]) -> Normalized[OwnerRow]:
    columns, updates = _columns(values, OWNERS)
    owner_rows = []

    seen_owners = set()
    for row_number, values_row in enumerate(values[1:], start=2):
        if not _has_values(values_row):
            continue

        raw_data = _raw_row(values_row, columns)
        category_name = str(raw_data['name'] or '').strip().capitalize()
        share_percent = as_decimal(raw_data['share_percent'])
        share_percent = min(max(share_percent or Decimal('70'), Decimal('0')), Decimal('100'))

        owner_data = {
            'name': _unique(category_name or f'Владелец {row_number}', seen_owners),
            'share_percent': share_percent,
            'is_active': False if not category_name else _as_bool(raw_data['is_active'], True),
        }

        owner_row = OwnerRow.model_validate(owner_data)
        owner_rows.append(owner_row)
        updates.extend(_row_updates(row_number, raw_data, owner_data, columns))

    return Normalized(owner_rows, updates)


def _normalize_prices(raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    retail = as_money(raw['retail_price'])
    wholesale = as_money(raw['wholesale_price'])
    discount = as_money(raw['discount_price'])

    is_unsafe = False
    if wholesale is None or wholesale < 0:
        wholesale, is_unsafe = Decimal('0'), True
    elif wholesale > MAX_MONEY:
        wholesale, is_unsafe = MAX_MONEY, True

    if retail is None or retail <= 0:
        candidates = [value for value in (discount, wholesale) if value and value > 0]
        retail, is_unsafe = max(candidates, default=Decimal('1')), True
    elif retail > MAX_MONEY:
        retail, is_unsafe = MAX_MONEY, True

    if discount is not None and (discount <= 0 or discount >= retail):
        discount = None

    return {
        'retail_price': retail,
        'wholesale_price': wholesale,
        'discount_price': discount,
    }, is_unsafe


def _default_owner(owners: list[OwnerRow]) -> OwnerRow:
    return next(
        (owner for owner in owners if owner.name.casefold() == 'булат'),
        next((owner for owner in owners if owner.is_active), owners[0]),
    )


def _normalize_products(
    values: list[list[Any]],
    category: CategoryRow,
    owners: list[OwnerRow],
    embedded_image_urls: Optional[dict[int, str]] = None,
    seen_skus: Optional[set[str]] = None,
) -> Normalized[CatalogRow]:
    columns, updates = _columns(values, PRODUCTS)
    owner_by_name = {owner.name.casefold(): owner for owner in owners}
    catalog_rows = []
    embedded_image_urls = embedded_image_urls or {}

    seen_skus = seen_skus if seen_skus is not None else set()
    for row_number, values_row in enumerate(values[1:], start=2):
        if not _has_values(values_row):
            continue

        raw_data = _raw_row(values_row, columns)
        product_name = str(raw_data['name'] or '').strip()
        if not product_name and not str(raw_data['sku'] or '').strip():
            continue

        product_sku = normalize_sku(raw_data['sku'], product_name, category.name)
        base_sku = product_sku

        sku_suffix = 2
        while product_sku.casefold() in seen_skus:
            sku_tail = f'-{sku_suffix}'
            product_sku = f'{base_sku[:100 - len(sku_tail)]}{sku_tail}'
            sku_suffix += 1

        seen_skus.add(product_sku.casefold())
        is_unsafe = not product_name
        product_name = product_name or f'Товар {product_sku}'
        prices, prices_unsafe = _normalize_prices(raw_data)
        is_unsafe |= prices_unsafe

        owner_name = str(raw_data['owner'] or '').strip().capitalize()
        owner = owner_by_name.get(owner_name.casefold()) or _default_owner(owners)

        sheet_data = {
            'sku': product_sku,
            'name': product_name,
            'description': str(raw_data['description'] or '').strip(),
            'characteristics': str(raw_data['characteristics'] or '').strip(),
            **prices,
            'image_url': (
                embedded_image_urls.get(row_number)
                or _image_reference(raw_data['image_url'])
            ),
            'is_active': False if is_unsafe else _as_bool(raw_data['is_active'], True),
            'is_popular': _as_bool(raw_data['is_popular'], False),
            'is_recommended': _as_bool(raw_data['is_recommended'], False),
            'owner': owner.name,
        }

        catalog_row = CatalogRow.model_validate({
            **sheet_data,
            'category': category.name,
            'category_image_url': category.image_url,
            'owner_share_percent': owner.share_percent,
        })

        catalog_rows.append(catalog_row.model_copy(update={
            'is_active': catalog_row.is_active and category.is_active and owner.is_active,
        }))

        writable_data = sheet_data
        if row_number in embedded_image_urls:
            writable_data = {
                field: value
                for field, value in sheet_data.items()
                if field != 'image_url'
            }

        updates.extend(_row_updates(row_number, raw_data, writable_data, columns))

    if not catalog_rows:
        raise CatalogValidationError('The products worksheet contains no products')

    return Normalized(catalog_rows, updates)


def _worksheet(spreadsheet: gspread.Spreadsheet, spec: SheetSpec) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(spec.title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=spec.title, rows=1000, cols=len(spec.columns), )


def _product_worksheets(spreadsheet: gspread.Spreadsheet) -> list[gspread.Worksheet]:
    return [
        worksheet
        for worksheet in spreadsheet.worksheets()
        if worksheet.title.casefold() not in CONTROL_SHEET_TITLES
    ]


def _sheet_range(title: str) -> str:
    return f"'{title.replace(chr(39), chr(39) * 2)}'"


def _worksheet_values(
    spreadsheet: gspread.Spreadsheet,
    worksheets: list[gspread.Worksheet],
) -> dict[str, list[list[Any]]]:
    response = spreadsheet.values_batch_get(
        [_sheet_range(worksheet.title) for worksheet in worksheets],
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


def _write(worksheet: gspread.Worksheet, updates: list[CellUpdate]) -> None:
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


def _validation(
    worksheet: gspread.Worksheet,
    range_name: str,
    kind: ValidationConditionType,
) -> dict[str, Any]:
    return {
        'setDataValidation': {
            'range': a1_range_to_grid_range(range_name, worksheet.id),
            'rule': {
                'condition': {
                    'type': kind.value,
                },
                'showCustomUi': True,
                'strict': True,
            },
        },
    }


def _apply_validations(
    spreadsheet: gspread.Spreadsheet,
    control_worksheets: dict[str, gspread.Worksheet],
    control_values: dict[str, list[list[Any]]],
) -> None:
    control_column_maps = {
        spec.title: _columns(control_values[spec.title], spec)[0]
        for spec in (OWNERS,)
    }

    requests = []
    for spec in (OWNERS,):
        worksheet = control_worksheets[spec.title]
        last_row = worksheet.row_count
        for field in spec.checkbox_fields:
            column = control_column_maps[spec.title][field]
            requests.append(_validation(
                worksheet,
                f'{gspread.utils.rowcol_to_a1(2, column)}:'
                f'{gspread.utils.rowcol_to_a1(last_row, column)}',
                ValidationConditionType.boolean,
            ))

    if requests:
        spreadsheet.batch_update({'requests': requests})


def _load_catalog() -> CatalogSource:
    if not CREDENTIALS_PATH.is_file():
        raise CatalogValidationError(f'Google credentials file not found: {CREDENTIALS_PATH}')

    try:
        client = gspread.service_account(filename=str(CREDENTIALS_PATH))
        spreadsheet = client.open(SPREADSHEET_TITLE)
        control_worksheets = {
            spec.title: _worksheet(spreadsheet, spec)
            for spec in (OWNERS,)
        }
        product_worksheets = _product_worksheets(spreadsheet)
        if not product_worksheets:
            raise CatalogValidationError(
                'Google spreadsheet contains no category worksheets',
            )

        all_catalog_worksheets = [*control_worksheets.values(), *product_worksheets]
        all_values = _worksheet_values(spreadsheet, all_catalog_worksheets)
        control_values = {
            title: all_values[title]
            for title in control_worksheets
        }
        product_values = {
            worksheet.title: all_values[worksheet.title]
            for worksheet in product_worksheets
        }

        if not _has_records(control_values[OWNERS.title]):
            control_values[OWNERS.title] = [
                list(OWNERS.columns),
                ['Диана', 70, True],
                ['Булат', 70, True],
            ]

            control_worksheets[OWNERS.title].update(
                range_name='A1', values=control_values[OWNERS.title],
                value_input_option=ValueInputOption.raw,
            )

        categories = [CategoryRow(name=worksheet.title) for worksheet in product_worksheets]
        category_by_name = {
            category.name.casefold(): category
            for category in categories
        }
        owners = _normalize_owners(control_values[OWNERS.title])

        image_urls = load_manifest()

        product_rows = []
        product_updates: dict[str, list[CellUpdate]] = {}
        corrected_product_rows = 0
        seen_skus: set[str] = set()
        for worksheet in product_worksheets:
            normalized = _normalize_products(
                product_values[worksheet.title],
                category_by_name[worksheet.title.casefold()],
                owners.rows,
                image_urls.get(worksheet.title),
                seen_skus,
            )
            product_rows.extend(normalized.rows)
            product_updates[worksheet.title] = normalized.updates
            corrected_product_rows += normalized.corrected_rows

        _write(control_worksheets[OWNERS.title], owners.updates)
        for worksheet in product_worksheets:
            _write(worksheet, product_updates[worksheet.title])

        _apply_validations(
            spreadsheet,
            control_worksheets,
            control_values,
        )

        return CatalogSource(
            products=product_rows,
            categories=categories,
            corrected_rows=(
                corrected_product_rows
                + owners.corrected_rows
            ),
        )

    except gspread.SpreadsheetNotFound as exc:
        raise CatalogValidationError(
            f'Google spreadsheet {SPREADSHEET_TITLE!r} was not found or not shared '
            'with the service account',
        ) from exc

    except CatalogValidationError:
        raise

    except Exception as exc:
        raise CatalogValidationError(
            f'Google Sheets catalog could not be loaded: {exc}',
        ) from exc


async def load_catalog_source() -> CatalogSource:
    return await asyncio.to_thread(_load_catalog)


def _load_store_settings() -> StoreSettings:
    if not CREDENTIALS_PATH.is_file():
        raise SettingsValidationError(
            f'Google credentials file not found: {CREDENTIALS_PATH}',
        )

    try:
        client = gspread.service_account(filename=str(CREDENTIALS_PATH))
        spreadsheet = client.open(SPREADSHEET_TITLE)
        worksheet = _worksheet(spreadsheet, SETTINGS)
        values = worksheet.get_all_values(
            value_render_option=ValueRenderOption.unformatted,
        )
        settings, updates = _normalize_settings(values)
        _write(worksheet, updates)
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


async def load_store_settings() -> StoreSettings:
    return await asyncio.to_thread(_load_store_settings)
