from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from rewire import config, logger, simple_plugin
from rewire_sqlmodel import session_context, transaction
from sqlmodel import col

from src.auth import get_init_data_user
from src.catalog import CatalogValidationError, sync_catalog
from src.models import (
    AvailabilityRequest,
    CartItem,
    Category,
    Favorite,
    Order,
    OrderItem,
    Product,
    ProductView,
    User,
)
from src.orders import (
    ORDER_STATUS_LABELS,
    create_order_from_cart,
    report_payment,
)


@config
class Config(BaseModel):
    products_path: str = 'assets/products.xlsx'


plugin = simple_plugin()
router = APIRouter()
templates = Jinja2Templates(directory='templates')
RequestUser = Annotated[User, Depends(get_init_data_user)]


class HealthResponse(BaseModel):
    status: str


class UpdateProfileRequest(BaseModel):
    first_name: str
    last_name: str
    birth_date: date
    phone_number: str

    @field_validator('first_name', 'last_name')
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2 or len(value) > 80:
            raise ValueError('Имя и фамилия должны содержать от 2 до 80 символов')
        return value

    @field_validator('birth_date')
    @classmethod
    def validate_birth_date(cls, value: date) -> date:
        if value >= date.today():
            raise ValueError('Укажите корректную дату рождения')
        return value

    @field_validator('phone_number')
    @classmethod
    def validate_phone(cls, value: str) -> str:
        value = value.strip()
        digits = ''.join(character for character in value if character.isdigit())
        if not 10 <= len(digits) <= 15:
            raise ValueError('Укажите корректный номер телефона')
        return value


class OkResponse(BaseModel):
    ok: bool


class ToggleFavoriteResponse(BaseModel):
    favorite: bool


class AddToCartRequest(BaseModel):
    quantity: int = 1


class AddToCartResponse(BaseModel):
    quantity: int


class SetCartQuantityRequest(BaseModel):
    quantity: int = Field(ge=1, le=999)


class AvailabilityRequestBody(BaseModel):
    quantity: int = Field(default=1, ge=1, le=999)


class AvailabilityResponse(BaseModel):
    request_id: int
    status: str


class CheckoutRequest(BaseModel):
    discount_mode: str = 'none'
    promo_code: str = ''
    coins_requested: Decimal = Field(default=Decimal('0'), ge=0)


class CheckoutResponse(BaseModel):
    order_id: int
    order_number: str
    paid_total: Decimal


class ReportPaymentResponse(BaseModel):
    ok: bool
    status: str


def _is_new(product: Product) -> bool:
    created_at = product.created_at
    if not created_at.tzinfo:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at >= datetime.now(timezone.utc) - timedelta(days=7)


async def _catalog_context(
    request: Request,
    q: str = '',
    category_id: Optional[int] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
) -> dict:
    query = Product.select().where(col(Product.is_active).is_(True))
    if q:
        query = query.where(col(Product.name).ilike(f'%{q.strip()}%'))
    if category_id:
        query = query.where(Product.category_id == category_id)
    if min_price:
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
        'request': request,
        'products': products,
        'categories': categories,
        'categories_by_id': {category.id: category for category in categories},
        'is_new': _is_new,
        'filters': {
            'q': q,
            'category_id': category_id,
            'min_price': min_price,
            'max_price': max_price,
        },
    }


@router.get('/health', response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status='ok')


@router.get('/', response_class=HTMLResponse)
@transaction(1)
async def home(request: Request) -> HTMLResponse:
    catalog_context = await _catalog_context(request)
    context = {
        **catalog_context,
        'popular': [
            product for product in catalog_context['products'] if product.is_popular
        ][:6],
        'recommended': [
            product
            for product in catalog_context['products']
            if product.is_recommended
        ][:6],
        'new_products': [
            product for product in catalog_context['products'] if _is_new(product)
        ][:6],
    }
    return templates.TemplateResponse(request=request, name='home.html', context=context)


@router.get('/catalog', response_class=HTMLResponse)
@transaction(1)
async def catalog(
    request: Request,
    q: str = '',
    category_id: Optional[int] = None,
    min_price: Optional[int] = Query(default=None, ge=0),
    max_price: Optional[int] = Query(default=None, ge=0),
) -> HTMLResponse:
    context = await _catalog_context(request, q, category_id, min_price, max_price)
    return templates.TemplateResponse(
        request=request,
        name='catalog.html',
        context=context,
    )


@router.get('/products/{sku}', response_class=HTMLResponse)
@transaction(1)
async def product_detail(request: Request, sku: str) -> HTMLResponse:
    product = await Product.select().filter_by(sku=sku, is_active=True).first()
    if not product:
        raise HTTPException(status_code=404, detail='Product not found')
    category = await Category.select().filter_by(id=product.category_id).first()

    return templates.TemplateResponse(
        request=request,
        name='product.html',
        context={
            'request': request,
            'product': product,
            'category': category,
            'is_new': _is_new(product),
        },
    )


@router.get('/favorites', response_class=HTMLResponse)
async def favorites_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name='collection.html',
        context={
            'request': request,
            'title': 'Избранное',
            'subtitle': 'Товары, которые вы сохранили',
            'endpoint': '/api/favorites',
        },
    )


@router.get('/recent', response_class=HTMLResponse)
async def recent_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name='collection.html',
        context={
            'request': request,
            'title': 'Недавно просмотренные',
            'subtitle': 'Быстро вернитесь к тому, что смотрели',
            'endpoint': '/api/recent',
        },
    )


@router.get('/cart', response_class=HTMLResponse)
async def cart_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name='cart.html',
        context={'request': request},
    )


@router.get('/profile', response_class=HTMLResponse)
async def profile_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name='profile.html',
        context={'request': request},
    )


@router.get('/orders', response_class=HTMLResponse)
async def orders_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name='orders.html',
        context={'request': request},
    )


@router.get('/orders/{order_id}', response_class=HTMLResponse)
async def order_page(request: Request, order_id: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name='order.html',
        context={'request': request, 'order_id': order_id},
    )


@router.get('/reviews', response_class=HTMLResponse)
async def reviews_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name='empty.html',
        context={
            'request': request,
            'title': 'Отзывы',
            'message': 'Раздел отзывов готовится. Скоро здесь появятся отзывы покупателей.',
        },
    )


@router.get('/api/favorites', response_class=HTMLResponse)
@transaction(1)
async def favorites_fragment(
    request: Request,
    user: RequestUser,
) -> HTMLResponse:
    favorites = await Favorite.select().filter_by(user_id=user.id).all()
    product_ids = [favorite.product_id for favorite in favorites]
    products = (
        list(await Product.select().where(col(Product.id).in_(product_ids)).all())
        if product_ids
        else []
    )
    categories = await Category.select().all()
    return templates.TemplateResponse(
        request=request,
        name='_collection_content.html',
        context={
            'request': request,
            'products': products,
            'categories_by_id': {category.id: category for category in categories},
            'is_new': _is_new,
        },
    )


@router.get('/api/recent', response_class=HTMLResponse)
@transaction(1)
async def recent_fragment(
    request: Request,
    user: RequestUser,
) -> HTMLResponse:
    views = list(
        await ProductView.select()
        .filter_by(user_id=user.id)
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
        name='_collection_content.html',
        context={
            'request': request,
            'products': products,
            'categories_by_id': {category.id: category for category in categories},
            'is_new': _is_new,
        },
    )


@router.get('/api/cart', response_class=HTMLResponse)
@transaction(1)
async def cart_fragment(
    request: Request,
    user: RequestUser,
) -> HTMLResponse:
    items = list(await CartItem.select().filter_by(user_id=user.id).all())
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
        name='_cart_content.html',
        context={
            'request': request,
            'items': items,
            'products_by_id': products_by_id,
            'total': total,
            'user': user,
        },
    )


@router.get('/api/profile', response_class=HTMLResponse)
async def profile_fragment(
    request: Request,
    user: RequestUser,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name='_profile_content.html',
        context={'request': request, 'user': user},
    )


@router.get('/api/orders', response_class=HTMLResponse)
@transaction(1)
async def orders_fragment(
    request: Request,
    user: RequestUser,
) -> HTMLResponse:
    orders = list(
        await Order.select()
        .filter_by(user_id=user.id)
        .order_by(Order.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request=request,
        name='_orders_content.html',
        context={
            'request': request,
            'orders': orders,
            'status_labels': ORDER_STATUS_LABELS,
        },
    )


@router.get('/api/orders/{order_id}', response_class=HTMLResponse)
@transaction(1)
async def order_fragment(
    request: Request,
    order_id: int,
    user: RequestUser,
) -> HTMLResponse:
    order = await Order.select().filter_by(id=order_id, user_id=user.id).first()
    if not order:
        raise HTTPException(status_code=404, detail='Заказ не найден')
    items = list(await OrderItem.select().filter_by(order_id=order.id).all())
    return templates.TemplateResponse(
        request=request,
        name='_order_content.html',
        context={
            'request': request,
            'order': order,
            'items': items,
            'status_labels': ORDER_STATUS_LABELS,
        },
    )


@router.post('/api/profile', response_model=OkResponse)
@transaction(1)
async def update_profile(
    request: UpdateProfileRequest,
    user: RequestUser,
) -> OkResponse:
    user.first_name = request.first_name
    user.last_name = request.last_name
    user.birth_date = request.birth_date
    user.phone_number = request.phone_number
    user.updated_at = datetime.now()
    user.add()
    return OkResponse(ok=True)


@router.post(
    '/api/products/{product_id}/view',
    response_model=OkResponse,
)
@transaction(1)
async def record_product_view(
    product_id: int,
    user: RequestUser,
) -> OkResponse:
    product = await Product.select().filter_by(id=product_id, is_active=True).first()
    if not product:
        raise HTTPException(status_code=404, detail='Product not found')

    view = await ProductView.select().filter_by(
        user_id=user.id,
        product_id=product_id,
    ).first()

    if not view:
        ProductView(user_id=user.id, product_id=product_id).add()
    else:
        view.viewed_at = datetime.now()
        view.add()

    product.views_count += 1
    product.add()

    return OkResponse(ok=True)


@router.post('/api/favorites/{product_id}', response_model=ToggleFavoriteResponse)
@transaction(1)
async def toggle_favorite(product_id: int, user: RequestUser) -> ToggleFavoriteResponse:
    product = await Product.select().filter_by(id=product_id, is_active=True).first()
    if not product:
        raise HTTPException(status_code=404, detail='Product not found')

    favorite = await Favorite.select().filter_by(
        user_id=user.id,
        product_id=product_id,
    ).first()

    if not favorite:
        Favorite(user_id=user.id, product_id=product_id).add()
        return ToggleFavoriteResponse(favorite=True)

    await favorite.delete()
    return ToggleFavoriteResponse(favorite=False)


@router.post('/api/cart/{product_id}', response_model=AddToCartResponse)
@transaction(1)
async def add_to_cart(
    product_id: int,
    request: AddToCartRequest,
    user: RequestUser,
) -> AddToCartResponse:
    if not 1 <= request.quantity <= 999:
        raise HTTPException(
            status_code=422,
            detail='Quantity must be between 1 and 999',
        )

    product = await Product.select().filter_by(id=product_id, is_active=True).first()
    if not product:
        raise HTTPException(status_code=404, detail='Product not found')

    item = await CartItem.select().filter_by(
        user_id=user.id,
        product_id=product_id,
    ).first()

    if not item:
        item = CartItem(
            user_id=user.id,
            product_id=product_id,
            quantity=request.quantity,
        ).add()
    else:
        item.quantity = min(item.quantity + request.quantity, 999)
        item.updated_at = datetime.now()
        item.add()

    product.cart_additions_count += 1
    product.add()
    return AddToCartResponse(quantity=item.quantity)


@router.put('/api/cart/{product_id}', response_model=AddToCartResponse)
@transaction(1)
async def set_cart_quantity(
    product_id: int,
    request: SetCartQuantityRequest,
    user: RequestUser,
) -> AddToCartResponse:
    item = await CartItem.select().filter_by(
        user_id=user.id,
        product_id=product_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail='Товар отсутствует в корзине')
    item.quantity = request.quantity
    item.updated_at = datetime.now()
    item.add()
    return AddToCartResponse(quantity=item.quantity)


@router.delete('/api/cart/{product_id}', response_model=OkResponse)
@transaction(1)
async def remove_from_cart(
    product_id: int,
    user: RequestUser,
) -> OkResponse:
    item = await CartItem.select().filter_by(
        user_id=user.id,
        product_id=product_id,
    ).first()
    if item:
        await item.delete()
    return OkResponse(ok=True)


@router.post(
    '/api/availability/{product_id}',
    response_model=AvailabilityResponse,
)
@transaction(1)
async def request_availability(
    product_id: int,
    request: AvailabilityRequestBody,
    user: RequestUser,
) -> AvailabilityResponse:
    product = await Product.select().filter_by(id=product_id, is_active=True).first()
    if not product:
        raise HTTPException(status_code=404, detail='Товар не найден')

    availability = await AvailabilityRequest.select().filter_by(
        user_id=user.id,
        product_id=product_id,
        status='pending',
    ).first()
    if availability:
        availability.requested_quantity = request.quantity
        availability.created_at = datetime.now()
        availability.add()
    else:
        availability = AvailabilityRequest(
            user_id=user.id,
            product_id=product_id,
            requested_quantity=request.quantity,
        ).add()
        await session_context.get().flush()
    return AvailabilityResponse(request_id=availability.id, status=availability.status)


@router.post('/api/orders', response_model=CheckoutResponse)
@transaction(1)
async def checkout(
    request: CheckoutRequest,
    user: RequestUser,
) -> CheckoutResponse:
    order = await create_order_from_cart(
        user=user,
        discount_mode=request.discount_mode,
        promo_code=request.promo_code,
        coins_requested=request.coins_requested,
    )
    return CheckoutResponse(
        order_id=order.id,
        order_number=order.number,
        paid_total=order.paid_total,
    )


@router.post(
    '/api/orders/{order_id}/report-payment',
    response_model=ReportPaymentResponse,
)
@transaction(1)
async def report_order_payment(
    order_id: int,
    user: RequestUser,
) -> ReportPaymentResponse:
    order = await Order.select().filter_by(id=order_id, user_id=user.id).first()
    if not order:
        raise HTTPException(status_code=404, detail='Заказ не найден')
    changed = await report_payment(order)
    return ReportPaymentResponse(ok=changed, status=order.status)


@plugin.setup()
def configure_web(app: FastAPI) -> None:
    app.mount('/static', StaticFiles(directory=Path('static')), name='static')
    app.include_router(router)


@plugin.setup()
@transaction(1)
async def import_catalog() -> None:
    try:
        report = await sync_catalog(Config.products_path)
        logger.info(
            'Catalog sync complete: created={}, updated={}, hidden={}',
            report.created,
            report.updated,
            report.hidden,
        )
    except CatalogValidationError as exc:
        logger.error('Catalog setup sync failed: {}', exc)
