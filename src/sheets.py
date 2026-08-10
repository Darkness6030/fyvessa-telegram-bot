import asyncio
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Generic, Optional, TypeVar

import gspread
from gspread.utils import (a1_range_to_grid_range, ValidationConditionType, ValueInputOption, ValueRenderOption)

from src.catalog import (
    CatalogRow,
    CatalogSource,
    CatalogValidationError,
    CategoryRow,
    OwnerRow,
)

SPREADSHEET_TITLE = 'Fyvessa Admin'
CREDENTIALS_PATH = Path('assets/credentials.json')
MONEY_QUANT = Decimal('0.01')
MAX_MONEY = Decimal('9999999999.99')


@dataclass(frozen=True)
class SheetSpec:
    title: str
    columns: tuple[str, ...]
    aliases: dict[str, str]
    checkbox_fields: tuple[str, ...] = ()


PRODUCTS = SheetSpec(
    title='products',
    columns=(
        'sku', 'name', 'category', 'description', 'characteristics',
        'retail_price', 'wholesale_price', 'discount_price', 'image_url',
        'is_active', 'is_popular', 'is_recommended', 'owner',
    ),
    aliases={
        'артикул': 'sku',
        'название': 'name',
        'категория': 'category',
        'описание': 'description',
        'характеристики': 'characteristics',
        'розничная цена': 'retail_price',
        'закупочная цена': 'wholesale_price',
        'оптовая цена': 'wholesale_price',
        'цена со скидкой': 'discount_price',
        'изображение': 'image_url',
        'активен': 'is_active',
        'популярный': 'is_popular',
        'рекомендуемый': 'is_recommended',
        'владелец': 'owner',
    },
    checkbox_fields=('is_active', 'is_popular', 'is_recommended'),
)
CATEGORIES = SheetSpec(
    title='categories',
    columns=('name', 'image_url', 'is_active'),
    aliases={
        'категория': 'name',
        'название': 'name',
        'изображение': 'image_url',
        'изображение категории': 'image_url',
        'активна': 'is_active',
        'активен': 'is_active',
    },
    checkbox_fields=('is_active',),
)
OWNERS = SheetSpec(
    title='owners',
    columns=('name', 'share_percent', 'is_active'),
    aliases={
        'владелец': 'name',
        'имя': 'name',
        'доля': 'share_percent',
        'процент': 'share_percent',
        'доля владельца': 'share_percent',
        'активен': 'is_active',
    },
    checkbox_fields=('is_active',),
)

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


def _as_decimal(value: Any) -> Optional[Decimal]:
    normalized = ('' if value is None else str(value)).strip().casefold()
    for token in ('\u00a0', ' ', '₽', 'рублей', 'рубля', 'руб.', 'руб', '%'):
        normalized = normalized.replace(token, '')

    if not normalized:
        return None

    try:
        result = Decimal(normalized.replace(',', '.'))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _as_money(value: Any) -> Optional[Decimal]:
    result = _as_decimal(value)
    if result is None or abs(result) > MAX_MONEY:
        return result

    try:
        return result.quantize(MONEY_QUANT)
    except InvalidOperation:
        return None


def _sheet_value(field: str, value: Any) -> Any:
    if field in {'retail_price', 'wholesale_price', 'discount_price', 'share_percent', }:
        if value is None:
            return ''

        return int(value) if value == value.to_integral_value() else float(value)

    if field in {'is_active', 'is_popular', 'is_recommended'}:
        return bool(value)

    return '' if value is None else str(value)


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
        CellUpdate(row_number, columns[field], _sheet_value(field, value))
        for field, value in normalized.items()
        if _changed(raw.get(field), _sheet_value(field, value))
    ]


def _unique(value: str, seen: set[str]) -> str:
    candidate = value
    suffix = 2
    while candidate.casefold() in seen:
        candidate = f'{value} {suffix}'
        suffix += 1

    seen.add(candidate.casefold())
    return candidate


def _normalize_sku(value: Any, name: str, category: str) -> str:
    original = str(value or '').strip()
    normalized = unicodedata.normalize('NFKD', original)
    normalized = normalized.encode('ascii', 'ignore').decode().upper()
    normalized = re.sub(r'[^A-Z0-9._-]+', '-', normalized).strip('-._')

    if original.isascii() and normalized:
        return normalized[:100]

    digest = hashlib.sha1(f'{name}\0{category}'.encode()).hexdigest()[:10].upper()
    return f'ITEM-{digest}'


def _normalize_categories(values: list[list[Any]]) -> Normalized[CategoryRow]:
    columns, updates = _columns(values, CATEGORIES)
    category_rows = []

    seen_categories = set()
    for row_number, values_row in enumerate(values[1:], start=2):
        if not _has_values(values_row):
            continue

        raw_data = _raw_row(values_row, columns)
        category_name = str(raw_data['name'] or '').strip()

        category_data = {
            'name': _unique(category_name or f'Категория {row_number}', seen_categories),
            'image_url': str(raw_data['image_url'] or '').strip() or None,
            'is_active': False if not category_name else _as_bool(raw_data['is_active'], True),
        }

        category_row = CategoryRow.model_validate(category_data)
        category_rows.append(category_row)
        updates.extend(_row_updates(row_number, raw_data, category_data, columns))

    return Normalized(category_rows, updates)


def _normalize_owners(values: list[list[Any]]) -> Normalized[OwnerRow]:
    columns, updates = _columns(values, OWNERS)
    owner_rows = []

    seen_owners = set()
    for row_number, values_row in enumerate(values[1:], start=2):
        if not _has_values(values_row):
            continue

        raw_data = _raw_row(values_row, columns)
        category_name = str(raw_data['name'] or '').strip().capitalize()
        share_percent = _as_decimal(raw_data['share_percent'])
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
    retail = _as_money(raw['retail_price'])
    wholesale = _as_money(raw['wholesale_price'])
    discount = _as_money(raw['discount_price'])

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


def _default_owner(category: str, owners: list[OwnerRow]) -> OwnerRow:
    preferred = 'Диана' if category.casefold() == 'духи' else 'Булат'
    return next(
        (owner for owner in owners if owner.name.casefold() == preferred.casefold()),
        next((owner for owner in owners if owner.is_active), owners[0]),
    )


def _normalize_products(
    values: list[list[Any]],
    categories: list[CategoryRow],
    owners: list[OwnerRow],
) -> Normalized[CatalogRow]:
    columns, updates = _columns(values, PRODUCTS)
    category_by_name = {category.name.casefold(): category for category in categories}
    owner_by_name = {owner.name.casefold(): owner for owner in owners}
    catalog_rows = []

    seen_skus = set()
    for row_number, values_row in enumerate(values[1:], start=2):
        if not _has_values(values_row):
            continue

        raw_data = _raw_row(values_row, columns)
        product_name = str(raw_data['name'] or '').strip()
        category_name = str(raw_data['category'] or '').strip() or 'Без категории'
        category = category_by_name.get(category_name.casefold()) or CategoryRow(
            name=category_name,
        )

        product_sku = _normalize_sku(raw_data['sku'], product_name, category.name)
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
        owner = owner_by_name.get(owner_name.casefold()) or _default_owner(
            category.name, owners,
        )

        sheet_data = {
            'sku': product_sku,
            'name': product_name,
            'category': category.name,
            'description': str(raw_data['description'] or '').strip(),
            'characteristics': str(raw_data['characteristics'] or '').strip(),
            **prices,
            'image_url': str(raw_data['image_url'] or '').strip() or None,
            'is_active': False if is_unsafe else _as_bool(raw_data['is_active'], True),
            'is_popular': _as_bool(raw_data['is_popular'], False),
            'is_recommended': _as_bool(raw_data['is_recommended'], False),
            'owner': owner.name,
        }

        catalog_row = CatalogRow.model_validate({
            **sheet_data,
            'category_image_url': category.image_url,
            'owner_share_percent': owner.share_percent,
        })

        catalog_rows.append(catalog_row.model_copy(update={
            'is_active': catalog_row.is_active and category.is_active and owner.is_active,
        }))

        updates.extend(_row_updates(row_number, raw_data, sheet_data, columns))

    if not catalog_rows:
        raise CatalogValidationError('The products worksheet contains no products')

    return Normalized(catalog_rows, updates)


def _product_category_names(values: list[list[Any]]) -> list[str]:
    columns, _ = _columns(values, PRODUCTS)
    return [
        str(_raw_row(row, columns)['category'] or '').strip() or 'Без категории'
        for row in values[1:]
        if _has_values(row)
    ]


def _worksheet(spreadsheet: gspread.Spreadsheet, spec: SheetSpec) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(spec.title)
    except gspread.WorksheetNotFound:
        if spec is PRODUCTS and len(spreadsheet.worksheets()) == 1:
            worksheet = spreadsheet.sheet1
            worksheet.update_title(spec.title)
            return worksheet

        return spreadsheet.add_worksheet(title=spec.title, rows=1000, cols=len(spec.columns), )


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


def _absolute_a1(row: int, column: int) -> str:
    match = re.fullmatch(r'([A-Z]+)(\d+)', gspread.utils.rowcol_to_a1(row, column))
    if match is None:
        raise ValueError('Could not build an A1 range')

    return f'${match.group(1)}${match.group(2)}'


def _validation(
    worksheet: gspread.Worksheet,
    range_name: str,
    kind: ValidationConditionType,
    values: Optional[list[str]] = None,
) -> dict[str, Any]:
    return {
        'setDataValidation': {
            'range': a1_range_to_grid_range(range_name, worksheet.id),
            'rule': {
                'condition': {
                    'type': kind.value,
                    'values': [
                        {'userEnteredValue': value} for value in (values or [])
                    ],
                },
                'showCustomUi': True,
                'strict': True,
            },
        },
    }


def _apply_validations(
    spreadsheet: gspread.Spreadsheet,
    worksheets: dict[str, gspread.Worksheet],
    values: dict[str, list[list[Any]]],
) -> None:
    column_maps = {
        spec.title: _columns(values[spec.title], spec)[0]
        for spec in (PRODUCTS, CATEGORIES, OWNERS)
    }

    requests = []
    for spec in (PRODUCTS, CATEGORIES, OWNERS):
        worksheet = worksheets[spec.title]
        last_row = worksheet.row_count
        for field in spec.checkbox_fields:
            column = column_maps[spec.title][field]
            requests.append(_validation(
                worksheet,
                f'{gspread.utils.rowcol_to_a1(2, column)}:'
                f'{gspread.utils.rowcol_to_a1(last_row, column)}',
                ValidationConditionType.boolean,
            ))

    product_sheet = worksheets[PRODUCTS.title]
    for field, reference in (('category', CATEGORIES), ('owner', OWNERS)):
        target_column = column_maps[PRODUCTS.title][field]
        source_column = column_maps[reference.title]['name']
        source_last_row = worksheets[reference.title].row_count
        source_range = (
            f"='{reference.title}'!{_absolute_a1(2, source_column)}:"
            f'{_absolute_a1(source_last_row, source_column)}'
        )

        requests.append(_validation(
            product_sheet,
            f'{gspread.utils.rowcol_to_a1(2, target_column)}:'
            f'{gspread.utils.rowcol_to_a1(product_sheet.row_count, target_column)}',
            ValidationConditionType.one_of_range,
            [source_range],
        ))

    spreadsheet.batch_update({'requests': requests})


def _load_catalog() -> CatalogSource:
    if not CREDENTIALS_PATH.is_file():
        raise CatalogValidationError(f'Google credentials file not found: {CREDENTIALS_PATH}')

    try:
        client = gspread.service_account(filename=str(CREDENTIALS_PATH))
        spreadsheet = client.open(SPREADSHEET_TITLE)
        worksheets = {
            spec.title: _worksheet(spreadsheet, spec)
            for spec in (PRODUCTS, CATEGORIES, OWNERS)
        }

        values = {
            title: worksheet.get_all_values(value_render_option=ValueRenderOption.unformatted)
            for title, worksheet in worksheets.items()
        }

        if not _has_records(values[OWNERS.title]):
            values[OWNERS.title] = [
                list(OWNERS.columns),
                ['Диана', 70, True],
                ['Булат', 70, True],
            ]

            worksheets[OWNERS.title].update(
                range_name='A1', values=values[OWNERS.title],
                value_input_option=ValueInputOption.raw,
            )

        if not _has_records(values[PRODUCTS.title]):
            values[PRODUCTS.title] = [list(PRODUCTS.columns)]
            worksheets[PRODUCTS.title].update(
                range_name='A1', values=values[PRODUCTS.title],
                value_input_option=ValueInputOption.raw,
            )

            _apply_validations(spreadsheet, worksheets, values)
            raise CatalogValidationError('Google products worksheet contained no rows; headers were created')

        categories = _normalize_categories(values[CATEGORIES.title])
        known_categories = {
            row.name.casefold()
            for row in categories.rows
        }

        next_row = max(len(values[CATEGORIES.title]) + 1, 2)
        for name in _product_category_names(values[PRODUCTS.title]):
            if name.casefold() in known_categories:
                continue

            row = CategoryRow(name=name)
            categories.rows.append(row)
            categories.updates.extend(
                CellUpdate(next_row, column, _sheet_value(field, getattr(row, field)))
                for column, field in enumerate(CATEGORIES.columns, start=1)
            )

            known_categories.add(name.casefold())
            next_row += 1

        owners = _normalize_owners(values[OWNERS.title])
        products = _normalize_products(
            values[PRODUCTS.title], categories.rows, owners.rows,
        )

        _write(worksheets[CATEGORIES.title], categories.updates)
        _write(worksheets[OWNERS.title], owners.updates)
        _write(worksheets[PRODUCTS.title], products.updates)
        _apply_validations(spreadsheet, worksheets, values)

        return CatalogSource(
            products=products.rows,
            categories=categories.rows,
            corrected_rows=(
                products.corrected_rows
                + categories.corrected_rows
                + owners.corrected_rows
            ),
        )

    except gspread.SpreadsheetNotFound as exc:
        raise CatalogValidationError(
            f'Google spreadsheet {SPREADSHEET_TITLE!r} was not found or not shared '
            'with the service account',
        ) from exc

    except Exception as exc:
        raise CatalogValidationError(
            f'Google Sheets catalog could not be loaded: {exc}',
        ) from exc


async def load_catalog_source() -> CatalogSource:
    return await asyncio.to_thread(_load_catalog)
