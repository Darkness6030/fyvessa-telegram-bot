from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from rewire import config, logger, simple_plugin
from rewire_sqlmodel import session_context, transaction

from src.admin_flow import (
    notify_availability_request,
    notify_payment_review,
)
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
from src.orders import (confirmed_cart_availability, create_order_from_cart, ORDER_STATUS_LABELS, report_payment)


@config
class Config(BaseModel):
    products_path: str = 'assets/products.xlsx'
    payment_details: str = 'Реквизиты для оплаты уточните в чате поддержки'


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


class ShopStateResponse(BaseModel):
    favorite_product_ids: list[int]
    cart_quantity: int


def _optional_nonnegative_int(value: str, field_name: str) -> Optional[int]:
    value = value.strip()
    if not value:
        return None

    try:
        parsed = int(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f'Поле «{field_name}» должно быть целым числом',
        ) from exc

    if parsed < 0:
        raise HTTPException(
            status_code=422,
            detail=f'Поле «{field_name}» не может быть отрицательным',
        )

    return parsed


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
    products = await Product.search(
        q, category_id, min_price, max_price,
    )
    categories = await Category.get_active()
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
    category_id: str = '',
    min_price: str = '',
    max_price: str = '',
) -> HTMLResponse:
    parsed_category_id = _optional_nonnegative_int(category_id, 'Категория')
    parsed_min_price = _optional_nonnegative_int(min_price, 'Цена от')
    parsed_max_price = _optional_nonnegative_int(max_price, 'Цена до')

    if (
        parsed_min_price is not None
        and parsed_max_price is not None
        and parsed_min_price > parsed_max_price
    ):
        raise HTTPException(
            status_code=422,
            detail='Цена от не может быть больше цены до',
        )
    context = await _catalog_context(
        request,
        q.strip(),
        parsed_category_id,
        parsed_min_price,
        parsed_max_price,
    )
    return templates.TemplateResponse(
        request=request,
        name='catalog.html',
        context=context,
    )


@router.get('/products/{sku}', response_class=HTMLResponse)
@transaction(1)
async def product_detail(request: Request, sku: str) -> HTMLResponse:
    product = await Product.get_by_sku(sku, active_only=True)
    if not product:
        raise HTTPException(status_code=404, detail='Product not found')
    category = await Category.get_by_id(product.category_id)

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


@router.get('/api/favorites', response_class=HTMLResponse)
@transaction(1)
async def favorites_fragment(request: Request, user: RequestUser) -> HTMLResponse:
    favorites = await Favorite.get_for_user(user.id)
    product_ids = [favorite.product_id for favorite in favorites]
    products = await Product.get_by_ids(product_ids)
    categories = await Category.get_all()
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
async def recent_fragment(request: Request, user: RequestUser) -> HTMLResponse:
    views = await ProductView.get_for_user(user.id)
    products_by_id = (
        {
            product.id: product
            for product in (
            await Product.get_by_ids([view.product_id for view in views])
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
    categories = await Category.get_all()
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
async def cart_fragment(request: Request, user: RequestUser) -> HTMLResponse:
    items = await CartItem.get_for_user(user.id)
    products_by_id = (
        {
            product.id: product
            for product in (
            await Product.get_by_ids([item.product_id for item in items])
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
    confirmations, missing_confirmations = await confirmed_cart_availability(user, items)
    availability_requests = await AvailabilityRequest.get_latest_for_products(
        user.id, [item.product_id for item in items],
    )
    latest_availability_by_product: dict[int, AvailabilityRequest] = {}
    for availability in availability_requests:
        latest_availability_by_product.setdefault(availability.product_id, availability)
    return templates.TemplateResponse(
        request=request,
        name='_cart_content.html',
        context={
            'request': request,
            'items': items,
            'products_by_id': products_by_id,
            'total': total,
            'user': user,
            'confirmed_product_ids': set(confirmations),
            'missing_confirmation_count': len(missing_confirmations),
            'availability_by_product': latest_availability_by_product,
            'has_pending_requests': any(
                availability.status == 'pending'
                for product_id, availability in latest_availability_by_product.items()
                if product_id in products_by_id
            ),
        },
    )


@router.get('/api/profile', response_class=HTMLResponse)
@transaction(1)
async def profile_fragment(request: Request, user: RequestUser) -> HTMLResponse:
    favorites_count = len(await Favorite.get_for_user(user.id))
    cart_items = await CartItem.get_for_user(user.id)
    orders_count = len(await Order.get_recent(user_id=user.id, limit=None))
    return templates.TemplateResponse(
        request=request,
        name='_profile_content.html',
        context={
            'request': request,
            'user': user,
            'favorites_count': favorites_count,
            'cart_quantity': sum(item.quantity for item in cart_items),
            'orders_count': orders_count,
        },
    )


@router.get('/api/orders', response_class=HTMLResponse)
@transaction(1)
async def orders_fragment(request: Request, user: RequestUser) -> HTMLResponse:
    orders = await Order.get_recent(user_id=user.id, limit=None)
    return templates.TemplateResponse(
        request=request,
        name='_orders_content.html',
        context={
            'request': request,
            'orders': orders,
            'status_labels': ORDER_STATUS_LABELS,
        },
    )


@router.get('/api/shop-state', response_model=ShopStateResponse)
@transaction(1)
async def shop_state(user: RequestUser) -> ShopStateResponse:
    favorites = await Favorite.get_for_user(user.id)
    cart_items = await CartItem.get_for_user(user.id)
    return ShopStateResponse(
        favorite_product_ids=[favorite.product_id for favorite in favorites],
        cart_quantity=sum(item.quantity for item in cart_items),
    )


@router.get('/api/orders/{order_id}', response_class=HTMLResponse)
@transaction(1)
async def order_fragment(request: Request, order_id: int, user: RequestUser) -> HTMLResponse:
    order = await Order.get_by_id(order_id, user.id)
    if not order:
        raise HTTPException(status_code=404, detail='Заказ не найден')
    items = await OrderItem.get_for_order(order.id)
    return templates.TemplateResponse(
        request=request,
        name='_order_content.html',
        context={
            'request': request,
            'order': order,
            'items': items,
            'status_labels': ORDER_STATUS_LABELS,
            'payment_details': Config.payment_details,
        },
    )


@router.post('/api/profile', response_model=OkResponse)
@transaction(1)
async def update_profile(request: UpdateProfileRequest, user: RequestUser) -> OkResponse:
    user.first_name = request.first_name
    user.last_name = request.last_name
    user.birth_date = request.birth_date
    user.phone_number = request.phone_number
    user.updated_at = datetime.now()
    user.add()
    return OkResponse(ok=True)


@router.post('/api/products/{product_id}/view', response_model=OkResponse)
@transaction(1)
async def record_product_view(product_id: int, user: RequestUser) -> OkResponse:
    product = await Product.get_by_id(product_id, active_only=True)
    if not product:
        raise HTTPException(status_code=404, detail='Product not found')

    view = await ProductView.get_for_product(user.id, product_id)

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
    product = await Product.get_by_id(product_id, active_only=True)
    if not product:
        raise HTTPException(status_code=404, detail='Product not found')

    favorite = await Favorite.get_for_product(user.id, product_id)

    if not favorite:
        Favorite(user_id=user.id, product_id=product_id).add()
        return ToggleFavoriteResponse(favorite=True)

    await favorite.delete()
    return ToggleFavoriteResponse(favorite=False)


@router.post('/api/cart/{product_id}', response_model=AddToCartResponse)
@transaction(1)
async def add_to_cart(product_id: int, request: AddToCartRequest, user: RequestUser) -> AddToCartResponse:
    if not 1 <= request.quantity <= 999:
        raise HTTPException(
            status_code=422,
            detail='Quantity must be between 1 and 999',
        )

    product = await Product.get_by_id(product_id, active_only=True)
    if not product:
        raise HTTPException(status_code=404, detail='Product not found')

    item = await CartItem.get_for_product(user.id, product_id)

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
async def set_cart_quantity(product_id: int, request: SetCartQuantityRequest, user: RequestUser) -> AddToCartResponse:
    item = await CartItem.get_for_product(user.id, product_id)
    if not item:
        raise HTTPException(status_code=404, detail='Товар отсутствует в корзине')
    item.quantity = request.quantity
    item.updated_at = datetime.now()
    item.add()
    return AddToCartResponse(quantity=item.quantity)


@router.delete('/api/cart/{product_id}', response_model=OkResponse)
@transaction(1)
async def remove_from_cart(product_id: int, user: RequestUser) -> OkResponse:
    item = await CartItem.get_for_product(user.id, product_id)
    if item:
        await item.delete()
    return OkResponse(ok=True)


@router.post('/api/availability/{product_id}', response_model=AvailabilityResponse)
@transaction(1)
async def request_availability(
    product_id: int,
    request: AvailabilityRequestBody,
    user: RequestUser,
) -> AvailabilityResponse:
    product = await Product.get_by_id(product_id, active_only=True)
    if not product:
        raise HTTPException(status_code=404, detail='Товар не найден')

    availability = await AvailabilityRequest.get_pending(user.id, product_id)
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

    delivered = await notify_availability_request(availability, product, user)
    if not delivered:
        raise HTTPException(
            status_code=503,
            detail='Админский чат временно недоступен. Попробуйте ещё раз позже',
        )

    return AvailabilityResponse(request_id=availability.id, status=availability.status)


@router.post('/api/orders', response_model=CheckoutResponse)
@transaction(1)
async def checkout(request: CheckoutRequest, user: RequestUser) -> CheckoutResponse:
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


@router.post('/api/orders/{order_id}/report-payment', response_model=ReportPaymentResponse)
@transaction(1)
async def report_order_payment(order_id: int, user: RequestUser) -> ReportPaymentResponse:
    order = await Order.get_by_id(order_id, user.id)
    if not order:
        raise HTTPException(status_code=404, detail='Заказ не найден')

    has_changed = await report_payment(order)
    if has_changed:
        delivered = await notify_payment_review(order, user)
        if not delivered:
            raise HTTPException(
                status_code=503,
                detail='Админский чат временно недоступен. Попробуйте ещё раз позже',
            )

    return ReportPaymentResponse(ok=has_changed, status=order.status)


@plugin.setup()
def include_router(app: FastAPI) -> None:
    app.mount('/static', StaticFiles(directory=Path('static')), name='static')
    app.include_router(router)


@plugin.setup()
@transaction(1)
async def import_catalog() -> None:
    try:
        report = await sync_catalog(Config.products_path)
        logger.info(
            'Catalog sync complete: created={}, updated={}, hidden={}',
            report.products_created,
            report.products_updated,
            report.products_hidden,
        )
    except CatalogValidationError as exc:
        logger.error('Catalog setup sync failed: {}', exc)
