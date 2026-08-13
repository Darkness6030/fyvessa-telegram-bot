import asyncio
import re
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlsplit

import gspread
from gspread.utils import ValueInputOption

from src.catalog import (
    CatalogRow,
    CatalogSource,
    CatalogValidationError,
    CategoryRow,
    OwnerRow,
)
from src.sheet_images import cache_spreadsheet_images
from src.sheet_utils import (as_bool, as_decimal, as_money, columns as resolve_columns, CREDENTIALS_PATH, ensure_checkboxes, get_worksheet, has_records, has_values, MAX_MONEY, normalize_sku, Normalized, OWNERS, OWNERS_SHEET, PRODUCTS, raw_row as extract_row, RESERVED_SHEET_TITLES, row_updates, SPREADSHEET_TITLE, unique, worksheet_values, write_updates)

TECHNICAL_CHARACTERISTIC_LABELS = frozenset({
    'доставка',
    'закупочная цена',
    'маржа',
    'наценка',
    'наценка доставка',
    'оптовая цена',
    'поставщик',
    'себестоимость',
    'owner',
    'supplier',
})


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


def _clean_characteristics(value: Any) -> str:
    result = []
    for line in str(value or '').splitlines():
        label = line.partition(':')[0].casefold()
        label = re.sub(r'[^a-zа-яё]+', ' ', label).strip()
        if label not in TECHNICAL_CHARACTERISTIC_LABELS and line.strip():
            result.append(line.strip())
    return '\n'.join(result)


def _normalize_owners(values: list[list[Any]]) -> Normalized[OwnerRow]:
    column_map, updates = resolve_columns(values, OWNERS)
    owner_rows = []

    seen_owners = set()
    for row_number, values_row in enumerate(values[1:], start=2):
        if not has_values(values_row):
            continue

        raw_data = extract_row(values_row, column_map)
        category_name = str(raw_data['name'] or '').strip().capitalize()
        share_percent = as_decimal(raw_data['share_percent'])
        share_percent = min(max(share_percent or Decimal('70'), Decimal('0')), Decimal('100'))

        if not category_name:
            continue

        owner_data = {
            'name': unique(category_name, seen_owners),
            'share_percent': share_percent,
        }

        owner_row = OwnerRow.model_validate(owner_data)
        owner_rows.append(owner_row)
        updates.extend(row_updates(row_number, raw_data, owner_data, column_map))

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
        owners[0],
    )


def _normalize_products(
    values: list[list[Any]],
    category: CategoryRow,
    owners: list[OwnerRow],
    embedded_image_urls: Optional[dict[int, str]] = None,
    seen_skus: Optional[set[str]] = None,
) -> Normalized[CatalogRow]:
    column_map, updates = resolve_columns(values, PRODUCTS)
    owner_by_name = {owner.name.casefold(): owner for owner in owners}
    catalog_rows = []
    embedded_image_urls = embedded_image_urls or {}

    seen_skus = seen_skus if seen_skus is not None else set()
    for row_number, values_row in enumerate(values[1:], start=2):
        if not has_values(values_row):
            continue

        raw_data = extract_row(values_row, column_map)
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
            'characteristics': _clean_characteristics(raw_data['characteristics']),
            **prices,
            'image_url': (
                embedded_image_urls.get(row_number)
                or _image_reference(raw_data['image_url'])
            ),
            'is_active': False if is_unsafe else as_bool(raw_data['is_active'], True),
            'is_popular': as_bool(raw_data['is_popular'], False),
            'is_new': as_bool(raw_data['is_new'], False),
            'owner': owner.name,
        }

        catalog_row = CatalogRow.model_validate({
            **sheet_data,
            'category': category.name,
            'owner_share_percent': owner.share_percent,
        })

        catalog_rows.append(catalog_row)

        writable_data = sheet_data
        if row_number in embedded_image_urls:
            writable_data = {
                field: value
                for field, value in sheet_data.items()
                if field != 'image_url'
            }

        updates.extend(row_updates(row_number, raw_data, writable_data, column_map))

    if not catalog_rows:
        raise CatalogValidationError('The products worksheet contains no products')

    return Normalized(catalog_rows, updates)


def _product_worksheets(spreadsheet: gspread.Spreadsheet) -> list[gspread.Worksheet]:
    return [
        worksheet
        for worksheet in spreadsheet.worksheets()
        if worksheet.title.casefold() not in RESERVED_SHEET_TITLES
    ]


def _load_catalog() -> CatalogSource:
    if not CREDENTIALS_PATH.is_file():
        raise CatalogValidationError(f'Google credentials file not found: {CREDENTIALS_PATH}')

    try:
        client = gspread.service_account(filename=str(CREDENTIALS_PATH))
        spreadsheet = client.open(SPREADSHEET_TITLE)
        owners_worksheet = get_worksheet(spreadsheet, OWNERS_SHEET, OWNERS)
        product_candidates = _product_worksheets(spreadsheet)
        if not product_candidates:
            raise CatalogValidationError(
                'Google spreadsheet contains no category worksheets',
            )

        all_catalog_worksheets = [owners_worksheet, *product_candidates]
        all_values = worksheet_values(spreadsheet, all_catalog_worksheets)
        owners_values = all_values[OWNERS_SHEET]
        product_worksheets = [
            worksheet
            for worksheet in product_candidates
            if has_records(all_values[worksheet.title])
        ]
        if not product_worksheets:
            raise CatalogValidationError(
                'Google spreadsheet contains no products',
            )

        if not has_records(owners_values):
            owners_values = [
                ['Владелец', 'Доля'],
                ['Диана', 70],
                ['Булат', 70],
            ]
            owners_worksheet.update(
                range_name='A1', values=owners_values,
                value_input_option=ValueInputOption.raw,
            )

        categories_by_sheet_id = {
            worksheet.id: CategoryRow(name=worksheet.title)
            for worksheet in product_worksheets
        }
        categories = list(categories_by_sheet_id.values())
        owners = _normalize_owners(owners_values)

        image_urls = cache_spreadsheet_images(
            client,
            spreadsheet.id,
            [worksheet.title for worksheet in product_worksheets],
        )

        product_rows = []
        product_updates = {}
        corrected_product_rows = 0

        seen_skus = set()
        for worksheet in product_worksheets:
            normalized = _normalize_products(
                all_values[worksheet.title],
                categories_by_sheet_id[worksheet.id],
                owners.rows,
                image_urls.get(worksheet.title),
                seen_skus,
            )
            product_rows.extend(normalized.rows)
            product_updates[worksheet.title] = normalized.updates
            corrected_product_rows += normalized.corrected_rows

        write_updates(owners_worksheet, owners.updates)
        checkbox_worksheets = []
        for worksheet in product_worksheets:
            write_updates(worksheet, product_updates[worksheet.title])
            column_map, _ = resolve_columns(all_values[worksheet.title], PRODUCTS)
            checkbox_worksheets.append((worksheet, column_map))
        ensure_checkboxes(spreadsheet, checkbox_worksheets, PRODUCTS)

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
