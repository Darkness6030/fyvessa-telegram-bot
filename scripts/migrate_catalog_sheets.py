"""Convert the catalog workbooks from tables.txt into Fyvessa Admin.

The source worksheets are copied with the Sheets copyTo API so over-grid images
remain real Google Sheets images. Only cell values are then rewritten into the
product schema consumed by src.sheets; the worksheet title is the category.
"""

import argparse
import hashlib
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import gspread
from gspread.utils import ValueInputOption, ValueRenderOption

from src.sheets import (
    CREDENTIALS_PATH,
    SPREADSHEET_TITLE,
)
from src.sheet_images import (
    IMAGE_DIR,
    MANIFEST_PATH,
    URL_PREFIX,
    embedded_images,
    export_xlsx,
    image_suffix,
    save_manifest,
)
from src.sheet_schema import (
    PRODUCTS,
    as_money,
    normalize_sku,
    sheet_value,
)


SPREADSHEET_ID_PATTERN = re.compile(r'/spreadsheets/d/([A-Za-z0-9_-]+)')


def _source_ids(path: Path) -> list[str]:
    result = []
    for line in path.read_text().splitlines():
        match = SPREADSHEET_ID_PATTERN.search(line.strip())
        if match:
            result.append(match.group(1))

    if len(result) != 2:
        raise ValueError(f'Expected two Google Sheets links in {path}, found {len(result)}')
    return result


def _text(value: Any) -> str:
    return str(value or '').strip()


def _at(row: list[Any], index: int) -> Any:
    return row[index] if index < len(row) else ''


def _characteristics(items: list[tuple[str, Any]]) -> str:
    lines = []
    for label, value in items:
        text = _text(value)
        if text and text not in {'—', '-'}:
            lines.append(f'{label}: {text}')
    return '\n'.join(lines)


def _unique_sku(name: str, category: str, seen: set[str]) -> str:
    base = normalize_sku('', name, category)
    sku = base
    suffix = 2
    while sku.casefold() in seen:
        tail = f'-{suffix}'
        sku = f'{base[:100 - len(tail)]}{tail}'
        suffix += 1
    seen.add(sku.casefold())
    return sku


def _product_values(data: dict[str, Any]) -> list[Any]:
    return [sheet_value(field, data.get(field)) for field in PRODUCTS.columns]


def _electronics_rows(
    values: list[list[Any]], category: str, seen_skus: set[str],
) -> list[list[Any]]:
    result = []
    for row in values[1:]:
        name = _text(_at(row, 3))
        if not name:
            result.append([])
            continue

        wholesale = as_money(_at(row, 11)) or as_money(_at(row, 6))
        retail = as_money(_at(row, 12)) or wholesale
        safe = bool(retail and retail > 0 and wholesale is not None and wholesale >= 0)
        data = {
            'image_url': None,
            'sku': _unique_sku(name, category, seen_skus),
            'name': name,
            'description': _text(_at(row, 4)),
            'characteristics': _characteristics([
                ('Бренд', _at(row, 2)),
                ('Вариант / цвет / длина', _at(row, 5)),
            ]),
            'retail_price': retail if safe else Decimal('1'),
            'wholesale_price': wholesale if safe else Decimal('0'),
            'discount_price': None,
            'is_active': safe,
            'is_popular': False,
            'is_recommended': False,
            'owner': 'Булат',
        }
        result.append(_product_values(data))
    return result


def _perfume_rows(
    values: list[list[Any]], category: str, seen_skus: set[str],
) -> list[list[Any]]:
    result = []
    has_header = bool(values and _text(_at(values[0], 1)).casefold() == 'название')
    for row in values[1:] if has_header else values:
        name = _text(_at(row, 1))
        if not name:
            result.append([])
            continue

        wholesale = as_money(_at(row, 3))
        retail = as_money(_at(row, 5))
        safe = bool(retail and retail > 0 and wholesale is not None and wholesale >= 0)
        data = {
            'image_url': None,
            'sku': _unique_sku(name, category, seen_skus),
            'name': name,
            'description': '',
            'characteristics': _characteristics([
                ('Объём', _at(row, 2)),
                ('Страна', _at(row, 6)),
                ('Пол', _at(row, 7)),
                ('Качество', _at(row, 8)),
                ('Наценка / доставка', _at(row, 4)),
            ]),
            'retail_price': retail if safe else Decimal('1'),
            'wholesale_price': wholesale if safe else Decimal('0'),
            'discount_price': None,
            'is_active': safe,
            'is_popular': False,
            'is_recommended': False,
            'owner': 'Диана',
        }
        result.append(_product_values(data))
    return result


def _copy_worksheet(
    client: gspread.Client,
    source: gspread.Spreadsheet,
    source_worksheet: gspread.Worksheet,
    target: gspread.Spreadsheet,
) -> gspread.Worksheet:
    response = client.http_client.request(
        'post',
        (
            f'https://sheets.googleapis.com/v4/spreadsheets/{source.id}/sheets/'
            f'{source_worksheet.id}:copyTo'
        ),
        json={'destinationSpreadsheetId': target.id},
    ).json()
    sheet_id = response['sheetId']
    return target.get_worksheet_by_id(sheet_id)


def _insert_header_row(spreadsheet: gspread.Spreadsheet, worksheet: gspread.Worksheet) -> None:
    spreadsheet.batch_update({
        'requests': [{
            'insertDimension': {
                'range': {
                    'sheetId': worksheet.id,
                    'dimension': 'ROWS',
                    'startIndex': 0,
                    'endIndex': 1,
                },
                'inheritFromBefore': False,
            },
        }],
    })


def _migrate_images(
    client: gspread.Client,
    sources: list[tuple[gspread.Spreadsheet, Any, bool]],
) -> int:
    manifest: dict[str, dict[int, str]] = {}
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    for source, _, source_has_headers in sources:
        embedded = embedded_images(export_xlsx(client, source.id))
        for worksheet in source.worksheets():
            values = worksheet.get_all_values(
                value_render_option=ValueRenderOption.unformatted,
            )
            first_name = _text(_at(values[0], 1)).casefold() if values else ''
            has_header = source_has_headers or first_name == 'название'
            exported_title = re.sub(r'[:\\/?*\[\]]', '', worksheet.title)[:31]
            images = embedded.get(worksheet.title) or embedded.get(exported_title) or {}
            row_urls = manifest.setdefault(worksheet.title, {})
            for (row, column), data in images.items():
                if column != 1:
                    continue
                target_row = row if has_header else row + 1
                digest = hashlib.sha256(data).hexdigest()[:24]
                filename = f'{digest}{image_suffix(data)}'
                path = IMAGE_DIR / filename
                if not path.exists():
                    path.write_bytes(data)
                row_urls[target_row] = f'{URL_PREFIX}/{filename}'

    save_manifest(manifest)
    return sum(len(rows) for rows in manifest.values())


def _unmerge_product_cells(
    client: gspread.Client,
    spreadsheet: gspread.Spreadsheet,
    product_titles: list[str],
) -> None:
    metadata = client.http_client.request(
        'get',
        f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet.id}',
        params={'fields': 'sheets(properties(title),merges)'},
    ).json()
    title_keys = {title.casefold() for title in product_titles}
    ranges = [
        merged_range
        for sheet in metadata.get('sheets', [])
        if sheet['properties']['title'].casefold() in title_keys
        for merged_range in sheet.get('merges', [])
    ]
    if ranges:
        spreadsheet.batch_update({
            'requests': [
                {'unmergeCells': {'range': merged_range}}
                for merged_range in ranges
            ],
        })


def _trim_product_columns(
    spreadsheet: gspread.Spreadsheet,
    worksheets: list[gspread.Worksheet],
) -> None:
    requests = [
        {
            'deleteDimension': {
                'range': {
                    'sheetId': worksheet.id,
                    'dimension': 'COLUMNS',
                    'startIndex': len(PRODUCTS.columns),
                    'endIndex': worksheet.col_count,
                },
            },
        }
        for worksheet in worksheets
        if worksheet.col_count > len(PRODUCTS.columns)
    ]
    if requests:
        spreadsheet.batch_update({'requests': requests})


def migrate(
    tables_path: Path,
    apply: bool,
    resume: bool = False,
    replace: bool = False,
) -> None:
    client = gspread.service_account(filename=str(CREDENTIALS_PATH))
    electronics_id, perfume_id = _source_ids(tables_path)
    sources = [
        (client.open_by_key(electronics_id), _electronics_rows, True),
        (client.open_by_key(perfume_id), _perfume_rows, False),
    ]
    target = client.open(SPREADSHEET_TITLE)
    existing_worksheets = {
        worksheet.title.casefold(): worksheet
        for worksheet in target.worksheets()
    }
    source_titles = [
        worksheet.title
        for source, _, _ in sources
        for worksheet in source.worksheets()
    ]
    conflicts = [title for title in source_titles if title.casefold() in existing_worksheets]
    if conflicts and not (resume or replace):
        raise RuntimeError(f'Target already contains category sheets: {conflicts}')
    if replace and conflicts:
        target.batch_update({
            'requests': [
                {
                    'deleteSheet': {
                        'sheetId': existing_worksheets[title.casefold()].id,
                    },
                }
                for title in conflicts
            ],
        })
        existing_worksheets = {
            title: worksheet
            for title, worksheet in existing_worksheets.items()
            if worksheet.title not in conflicts
        }

    seen_skus: set[str] = set()
    converted = []
    for source, converter, source_has_headers in sources:
        for source_worksheet in source.worksheets():
            values = source_worksheet.get_all_values(
                value_render_option=ValueRenderOption.unformatted,
            )
            rows = converter(values, source_worksheet.title, seen_skus)
            first_name = _text(_at(values[0], 1)).casefold() if values else ''
            has_header = source_has_headers or first_name == 'название'
            converted.append((source, source_worksheet, rows, has_header))

    print(f'Categories: {len(converted)}')
    product_count = sum(
        bool(row and len(row) > 2 and _text(row[2]))
        for _, _, rows, _ in converted
        for row in rows
    )
    print(f'Products: {product_count}')
    if not apply:
        print('Dry run only; pass --apply to change Google Sheets.')
        return

    if replace or not MANIFEST_PATH.is_file():
        print(f'Images cached: {_migrate_images(client, sources)}')

    prepared = []
    rename_requests = []
    for source, source_worksheet, rows, has_header in converted:
        worksheet = existing_worksheets.get(source_worksheet.title.casefold())
        created = worksheet is None
        if created:
            worksheet = _copy_worksheet(client, source, source_worksheet, target)
            rename_requests.append({
                'updateSheetProperties': {
                    'properties': {
                        'sheetId': worksheet.id,
                        'title': source_worksheet.title,
                    },
                    'fields': 'title',
                },
            })
        if created and not has_header:
            _insert_header_row(target, worksheet)

        prepared.append((source, source_worksheet, worksheet, rows, created))

    if rename_requests:
        target.batch_update({'requests': rename_requests})
        worksheets_by_id = {worksheet.id: worksheet for worksheet in target.worksheets()}
        prepared = [
            (source, source_worksheet, worksheets_by_id[worksheet.id], rows, created)
            for source, source_worksheet, worksheet, rows, created in prepared
        ]

    _unmerge_product_cells(client, target, source_titles)

    last_column = gspread.utils.rowcol_to_a1(1, len(PRODUCTS.columns))[:-1]
    for source, source_worksheet, worksheet, rows, created in prepared:
        worksheet.batch_clear([
            f'B1:{last_column}{max(worksheet.row_count, len(rows) + 1)}',
        ])
        worksheet.batch_update(
            [
                {'range': 'A1', 'values': [[PRODUCTS.columns[0]]]},
                {
                    'range': 'B1',
                    'values': [
                        list(PRODUCTS.columns[1:]),
                        *[
                            row[1:] if row else [''] * (len(PRODUCTS.columns) - 1)
                            for row in rows
                        ],
                    ],
                },
                {
                    'range': 'L2',
                    'values': [
                        [row[-1] if row else '']
                        for row in rows
                    ],
                },
            ],
            value_input_option=ValueInputOption.raw,
        )
        copied_products = sum(
            bool(row and len(row) > 2 and _text(row[2]))
            for row in rows
        )
        print(
            f'Copied {source.title} / {source_worksheet.title}: '
            f'{copied_products}',
        )

    _trim_product_columns(
        target,
        [worksheet for _, _, worksheet, _, _ in prepared],
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--tables', type=Path, default=Path('tables.txt'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    if args.resume and args.replace:
        parser.error('--resume and --replace cannot be used together')
    migrate(args.tables, args.apply, args.resume, args.replace)


if __name__ == '__main__':
    main()
