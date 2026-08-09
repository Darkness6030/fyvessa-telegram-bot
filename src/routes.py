from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from rewire import config, logger, simple_plugin
from rewire_sqlmodel import transaction
from sqlmodel import col

from src.auth import get_init_data_user
from src.catalog import CatalogValidationError, sync_catalog
from src.models import CartItem, Category, Customer, Favorite, Product, ProductView, utcnow


@config
class Config(BaseModel):
    products_path: str = "assets/products.xlsx"


plugin = simple_plugin()
router = APIRouter()
templates = Jinja2Templates(directory="templates")
RequestCustomer = Annotated[Customer, Depends(get_init_data_user)]


class QuantityRequest(BaseModel):
    quantity: int = 1


class ProfileRequest(BaseModel):
    first_name: str
    last_name: str
    birth_date: date
    phone_number: str


def _is_new(product: Product) -> bool:
    created_at = product.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at >= datetime.now(timezone.utc) - timedelta(days=7)


async def _catalog_context(
    request: Request,
    q: str = "",
    category_id: Optional[int] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
) -> dict:
    query = Product.select().where(col(Product.is_active).is_(True))
    if q:
        query = query.where(col(Product.name).ilike(f"%{q.strip()}%"))
    if category_id is not None:
        query = query.where(Product.category_id == category_id)
    if min_price is not None:
        query = query.where(Product.retail_price >= min_price)
    if max_price is not None:
        query = query.where(Product.retail_price <= max_price)

    products = list(await query.order_by(Product.name).all())
    categories = list(
        await Category.select()
        .where(col(Category.is_active).is_(True))
        .order_by(Category.name)
        .all()
    )
    return {
        "request": request,
        "products": products,
        "categories": categories,
        "categories_by_id": {category.id: category for category in categories},
        "is_new": _is_new,
        "filters": {
            "q": q,
            "category_id": category_id,
            "min_price": min_price,
            "max_price": max_price,
        },
    }


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
@transaction(1)
async def home(request: Request):
    context = await _catalog_context(request)
    context.update(
        popular=[product for product in context["products"] if product.is_popular][:6],
        recommended=[
            product for product in context["products"] if product.is_recommended
        ][:6],
        new_products=[product for product in context["products"] if _is_new(product)][:6],
    )
    return templates.TemplateResponse(request=request, name="home.html", context=context)


@router.get("/catalog", response_class=HTMLResponse)
@transaction(1)
async def catalog(
    request: Request,
    q: str = "",
    category_id: Optional[int] = None,
    min_price: Optional[int] = Query(default=None, ge=0),
    max_price: Optional[int] = Query(default=None, ge=0),
):
    context = await _catalog_context(request, q, category_id, min_price, max_price)
    return templates.TemplateResponse(request=request, name="catalog.html", context=context)


@router.get("/products/{sku}", response_class=HTMLResponse)
@transaction(1)
async def product_detail(request: Request, sku: str):
    product = await Product.select().filter_by(sku=sku, is_active=True).first()
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    category = await Category.select().filter_by(id=product.category_id).first()

    return templates.TemplateResponse(
        request=request,
        name="product.html",
        context={
            "request": request,
            "product": product,
            "category": category,
            "is_new": _is_new(product),
        },
    )


@router.get("/favorites", response_class=HTMLResponse)
async def favorites_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="collection.html",
        context={
            "request": request,
            "title": "Избранное",
            "subtitle": "Товары, которые вы сохранили",
            "endpoint": "/api/favorites",
        },
    )


@router.get("/recent", response_class=HTMLResponse)
async def recent_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="collection.html",
        context={
            "request": request,
            "title": "Недавно просмотренные",
            "subtitle": "Быстро вернитесь к тому, что смотрели",
            "endpoint": "/api/recent",
        },
    )


@router.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="cart.html",
        context={"request": request},
    )


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={"request": request},
    )


@router.get("/reviews", response_class=HTMLResponse)
async def reviews_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="empty.html",
        context={
            "request": request,
            "title": "Отзывы",
            "message": "Раздел отзывов готовится. Скоро здесь появятся отзывы покупателей.",
        },
    )


@router.get("/api/favorites", response_class=HTMLResponse)
@transaction(1)
async def favorites_fragment(
    request: Request,
    customer: RequestCustomer,
):
    favorites = await Favorite.select().filter_by(customer_id=customer.id).all()
    product_ids = [favorite.product_id for favorite in favorites]
    products = (
        list(await Product.select().where(col(Product.id).in_(product_ids)).all())
        if product_ids
        else []
    )
    categories = await Category.select().all()
    return templates.TemplateResponse(
        request=request,
        name="_collection_content.html",
        context={
            "request": request,
            "products": products,
            "categories_by_id": {category.id: category for category in categories},
            "is_new": _is_new,
        },
    )


@router.get("/api/recent", response_class=HTMLResponse)
@transaction(1)
async def recent_fragment(request: Request, customer: RequestCustomer):
    views = list(
        await ProductView.select()
        .filter_by(customer_id=customer.id)
        .order_by(ProductView.viewed_at.desc())
        .all()
    )[:20]
    products_by_id = (
        {
            product.id: product
            for product in (
                await Product.select()
                .where(col(Product.id).in_([view.product_id for view in views]))
                .all()
            )
        }
        if views
        else {}
    )
    products = [
        products_by_id[view.product_id]
        for view in views
        if view.product_id in products_by_id
    ]
    categories = await Category.select().all()
    return templates.TemplateResponse(
        request=request,
        name="_collection_content.html",
        context={
            "request": request,
            "products": products,
            "categories_by_id": {category.id: category for category in categories},
            "is_new": _is_new,
        },
    )


@router.get("/api/cart", response_class=HTMLResponse)
@transaction(1)
async def cart_fragment(request: Request, customer: RequestCustomer):
    items = list(await CartItem.select().filter_by(customer_id=customer.id).all())
    products_by_id = (
        {
            product.id: product
            for product in (
                await Product.select()
                .where(col(Product.id).in_([item.product_id for item in items]))
                .all()
            )
        }
        if items
        else {}
    )
    total = sum(
        products_by_id[item.product_id].current_price * item.quantity
        for item in items
        if item.product_id in products_by_id
    )
    return templates.TemplateResponse(
        request=request,
        name="_cart_content.html",
        context={
            "request": request,
            "items": items,
            "products_by_id": products_by_id,
            "total": total,
        },
    )


@router.get("/api/profile", response_class=HTMLResponse)
async def profile_fragment(request: Request, customer: RequestCustomer):
    return templates.TemplateResponse(
        request=request,
        name="_profile_content.html",
        context={"request": request, "customer": customer},
    )


@router.post("/api/profile")
@transaction(1)
async def update_profile(payload: ProfileRequest, customer: RequestCustomer):
    customer.first_name = payload.first_name
    customer.last_name = payload.last_name
    customer.birth_date = payload.birth_date
    customer.phone_number = payload.phone_number
    customer.updated_at = utcnow()
    customer.add()
    return {"ok": True}


@router.post("/api/products/{product_id}/view")
@transaction(1)
async def record_product_view(product_id: int, customer: RequestCustomer):
    product = await Product.select().filter_by(id=product_id, is_active=True).first()
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    view = await ProductView.select().filter_by(
        customer_id=customer.id,
        product_id=product_id,
    ).first()
    if view is None:
        ProductView(customer_id=customer.id, product_id=product_id).add()
    else:
        view.viewed_at = utcnow()
        view.add()
    product.views_count += 1
    product.add()
    return {"ok": True}


@router.post("/api/favorites/{product_id}")
@transaction(1)
async def toggle_favorite(product_id: int, customer: RequestCustomer):
    product = await Product.select().filter_by(id=product_id, is_active=True).first()
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    favorite = await Favorite.select().filter_by(
        customer_id=customer.id, product_id=product_id
    ).first()
    if favorite is None:
        Favorite(customer_id=customer.id, product_id=product_id).add()
        return {"favorite": True}
    await favorite.delete()
    return {"favorite": False}


@router.post("/api/cart/{product_id}")
@transaction(1)
async def add_to_cart(
    product_id: int,
    payload: QuantityRequest,
    customer: RequestCustomer,
):
    if not 1 <= payload.quantity <= 999:
        raise HTTPException(status_code=422, detail="Quantity must be between 1 and 999")
    product = await Product.select().filter_by(id=product_id, is_active=True).first()
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    item = await CartItem.select().filter_by(
        customer_id=customer.id, product_id=product_id
    ).first()
    if item is None:
        item = CartItem(
            customer_id=customer.id,
            product_id=product_id,
            quantity=payload.quantity,
        ).add()
    else:
        item.quantity = min(item.quantity + payload.quantity, 999)
        item.updated_at = utcnow()
        item.add()
    product.cart_additions_count += 1
    product.add()
    return {"quantity": item.quantity}


@router.delete("/api/cart/{product_id}")
@transaction(1)
async def remove_from_cart(product_id: int, customer: RequestCustomer):
    item = await CartItem.select().filter_by(
        customer_id=customer.id, product_id=product_id
    ).first()
    if item is not None:
        await item.delete()
    return {"ok": True}


@plugin.setup()
def configure_web(app: FastAPI) -> None:
    app.mount("/static", StaticFiles(directory=Path("static")), name="static")
    app.include_router(router)

    @app.on_event("startup")
    @transaction(1)
    async def import_catalog_on_startup() -> None:
        try:
            report = await sync_catalog(Config.products_path)
            logger.info(
                "Catalog sync complete: created={}, updated={}, hidden={}",
                report.created,
                report.updated,
                report.hidden,
            )
        except CatalogValidationError as exc:
            logger.error("Catalog startup sync failed: {}", exc)
