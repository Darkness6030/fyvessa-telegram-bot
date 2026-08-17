from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field as PydanticField, field_validator
from rewire import logger, simple_plugin
from rewire_sqlmodel import session_context, transaction

from src.models import Category, Product

plugin = simple_plugin()


class CatalogValidationError(ValueError):
    pass


class CategoryRow(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = PydanticField(min_length=1)


class OwnerRow(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = PydanticField(min_length=1)
    share_percent: Decimal = PydanticField(default=Decimal('70'), ge=0, le=100)


class CatalogRow(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    sku: str = PydanticField(min_length=1, max_length=100)
    name: str = PydanticField(min_length=1)
    category: str = PydanticField(min_length=1)
    description: str = ''
    characteristics: str = ''
    retail_price: Decimal = PydanticField(gt=0, decimal_places=2)
    wholesale_price: Decimal = PydanticField(ge=0, decimal_places=2)
    discount_price: Optional[Decimal] = PydanticField(default=None, gt=0, decimal_places=2)
    image_url: Optional[str] = None
    is_active: bool = True
    is_popular: bool = False
    is_new: bool = False
    owner: str = 'Булат'
    owner_share_percent: Decimal = PydanticField(default=Decimal('70'), ge=0, le=100)

    @field_validator('discount_price')
    @classmethod
    def discount_must_be_lower(cls, value: Optional[Decimal], info):
        retail_price = info.data.get('retail_price')
        if value is not None and retail_price is not None and value >= retail_price:
            raise ValueError('discount_price must be lower than retail_price')
        return value


@dataclass(frozen=True)
class CatalogSource:
    products: list[CatalogRow]
    categories: list[CategoryRow]
    corrected_rows: int = 0


@dataclass(frozen=True)
class SyncReport:
    products_created: int = 0
    products_updated: int = 0
    products_hidden: int = 0
    categories_created: int = 0
    rows_corrected: int = 0


@transaction(1)
async def _apply_catalog(source: CatalogSource) -> SyncReport:
    session = session_context.get()
    current_date = datetime.now()

    database_categories = {
        category.name.casefold(): category
        for category in await Category.get_all()
    }

    database_products = {
        product.sku.casefold(): product
        for product in await Product.get_all()
    }

    source_category_names = {
        category.name.casefold()
        for category in source.categories
    }

    source_skus = {product.sku.casefold() for product in source.products}
    created_products = updated_products = hidden_products = created_categories = 0

    for row in source.categories:
        category = database_categories.get(row.name.casefold())
        if category is None:
            category = Category(name=row.name).add()
            await session.flush()
            database_categories[row.name.casefold()] = category
            created_categories += 1
            continue

        category.is_active = True
        category.updated_at = current_date
        category.add()

    for key, category in database_categories.items():
        if key not in source_category_names and category.is_active:
            category.is_active = False
            category.updated_at = current_date
            category.add()

    for row in source.products:
        category = database_categories[row.category.casefold()]
        values = row.model_dump(exclude={'category'}, exclude_none=False)

        values.update(category_id=category.id, updated_at=current_date)
        product = database_products.get(row.sku.casefold())
        if product is None:
            Product(**values).add()
            created_products += 1
        else:
            product.sqlmodel_update(values)
            product.add()
            updated_products += 1

    for sku, product in database_products.items():
        if sku not in source_skus and product.is_active:
            product.is_active = False
            product.updated_at = current_date
            product.add()
            hidden_products += 1

    return SyncReport(
        products_created=created_products,
        products_updated=updated_products,
        products_hidden=hidden_products,
        categories_created=created_categories,
        rows_corrected=source.corrected_rows,
    )


async def sync_catalog() -> SyncReport:
    from src.sheets import load_catalog_source
    return await _apply_catalog(await load_catalog_source())


# @plugin.setup()
async def import_catalog() -> None:
    try:
        report = await sync_catalog()
        logger.info(
            'Catalog sync complete: created={}, updated={}, hidden={}, corrected={}',
            report.products_created,
            report.products_updated,
            report.products_hidden,
            report.rows_corrected,
        )
    except CatalogValidationError as exc:
        logger.error('Catalog setup sync failed: {}', exc)
