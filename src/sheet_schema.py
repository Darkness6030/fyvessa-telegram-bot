import hashlib
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

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
        'image_url', 'sku', 'name', 'description', 'characteristics',
        'retail_price', 'wholesale_price', 'discount_price',
        'is_active', 'is_popular', 'is_recommended', 'owner',
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
        'рекомендуемый': 'is_recommended',
        'владелец': 'owner',
    },
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

SETTINGS = SheetSpec(
    title='settings',
    columns=('key', 'value', 'description'),
    aliases={
        'ключ': 'key',
        'значение': 'value',
        'описание': 'description',
    },
)


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

    if field in {'is_active', 'is_popular', 'is_recommended'}:
        return bool(value)

    return '' if value is None else str(value)
