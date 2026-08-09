from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException
from rewire_sqlmodel import session_context

from src.models import (
    AvailabilityRequest,
    CartItem,
    Category,
    CoinTransaction,
    Order,
    OrderItem,
    Product,
    Promocode,
    User,
)
from src.pricing import calculate_pricing, money, PricingLine

ORDER_STATUS_LABELS = {
    'awaiting_payment': 'Ожидает оплаты',
    'payment_review': 'Оплата проверяется',
    'paid': 'Оплачен',
    'completed': 'Завершён',
    'cancelled': 'Отменён',
}


async def confirmed_cart_availability(user: User, items: Optional[list[CartItem]] = None) -> tuple[dict[int, AvailabilityRequest], list[CartItem]]:
    cart_items = items or await CartItem.get_for_user(user.id)
    if not cart_items:
        return {}, []

    product_ids = [item.product_id for item in cart_items]
    requests = await AvailabilityRequest.get_latest_for_products(user.id, product_ids)
    latest_by_product: dict[int, AvailabilityRequest] = {}
    for availability in requests:
        latest_by_product.setdefault(availability.product_id, availability)

    confirmed: dict[int, AvailabilityRequest] = {}
    missing: list[CartItem] = []
    for item in cart_items:
        availability = latest_by_product.get(item.product_id)
        if (
            availability
            and availability.status == 'available'
            and (availability.requested_quantity or 0) >= item.quantity
            and (availability.available_quantity or 0) >= item.quantity
        ):
            confirmed[item.product_id] = availability
        else:
            missing.append(item)

    return confirmed, missing


async def create_order_from_cart(
    user: User,
    discount_mode: str,
    promo_code: str,
    coins_requested: Decimal,
) -> Order:
    if not user.is_registered:
        raise HTTPException(
            status_code=409,
            detail='Заполните имя, фамилию, дату рождения и телефон в профиле',
        )

    items = await CartItem.get_for_user(user.id)
    if not items:
        raise HTTPException(status_code=409, detail='Корзина пуста')

    confirmations, missing_confirmations = await confirmed_cart_availability(user, items)
    if missing_confirmations:
        raise HTTPException(
            status_code=409,
            detail='Сначала подтвердите наличие нужного количества всех товаров в корзине',
        )

    products = await Product.get_by_ids(
        [item.product_id for item in items],
        active_only=True,
    )

    products_by_id = {product.id: product for product in products}
    if len(products_by_id) != len({item.product_id for item in items}):
        raise HTTPException(
            status_code=409,
            detail='Один из товаров больше недоступен. Удалите его из корзины',
        )

    if discount_mode not in {'none', 'personal', 'promo'}:
        raise HTTPException(status_code=422, detail='Неизвестный тип скидки')

    promocode = None
    personal_percent = Decimal('0')
    promo_percent = Decimal('0')

    if discount_mode == 'personal':
        personal_percent = user.personal_discount_percent
    elif discount_mode == 'promo':
        promocode = await Promocode.get_by_code(promo_code, active_only=True)
        if not promocode:
            raise HTTPException(status_code=422, detail='Промокод не найден или отключён')

        promo_percent = promocode.user_discount_percent

    if coins_requested < 0:
        raise HTTPException(status_code=422, detail='Количество коинов не может быть отрицательным')

    if coins_requested > user.coin_balance:
        raise HTTPException(status_code=422, detail='Недостаточно коинов')

    pricing = calculate_pricing(
        [
            PricingLine(
                quantity=item.quantity,
                retail_price=products_by_id[item.product_id].retail_price,
                sale_price=products_by_id[item.product_id].current_price,
                wholesale_price=products_by_id[item.product_id].wholesale_price,
                owner=products_by_id[item.product_id].owner,
                owner_share_percent=products_by_id[item.product_id].owner_share_percent,
            )
            for item in items
        ],
        personal_percent=personal_percent,
        promo_percent=promo_percent,
        coins_requested=coins_requested,
    )

    now = datetime.now()
    order = Order(
        number=f'FY-{now:%y%m%d}-{uuid4().hex[:6].upper()}',
        user_id=user.id,
        status='awaiting_payment',
        payment_status='not_paid',
        promo_code_id=promocode.id if promocode else None,
        discount_mode=discount_mode,
        product_discount_total=pricing.product_discount,
        personal_discount_percent=personal_percent,
        promo_discount_percent=promo_percent,
        coins_used=pricing.coins_used,
        subtotal=pricing.retail_subtotal,
        paid_total=pricing.paid_total,
        wholesale_total=pricing.wholesale_total,
    ).add()
    await session_context.get().flush()

    categories = await Category.get_all()
    category_names = {category.id: category.name for category in categories}
    for item in items:
        product = products_by_id[item.product_id]
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=item.quantity,
            sku_snapshot=product.sku,
            product_name_snapshot=product.name,
            category_snapshot=category_names.get(product.category_id, 'Без категории'),
            retail_price_snapshot=product.retail_price,
            sale_price_snapshot=product.current_price,
            wholesale_price_snapshot=product.wholesale_price,
            owner_snapshot=product.owner,
            owner_share_percent_snapshot=product.owner_share_percent,
        ).add()

    if pricing.coins_used:
        user.coin_balance = money(user.coin_balance - pricing.coins_used)
        user.updated_at = now
        user.add()
        CoinTransaction(
            user_id=user.id,
            order_id=order.id,
            amount=-pricing.coins_used,
            balance_after=user.coin_balance,
            reason=f'Списание для заказа {order.number}',
        ).add()

    for item in items:
        await item.delete()

    for availability in confirmations.values():
        availability.status = 'used'
        availability.add()

    return order


async def report_payment(order: Order) -> bool:
    if order.status == 'payment_review':
        return False

    if order.status != 'awaiting_payment':
        raise HTTPException(status_code=409, detail='Статус этого заказа уже изменён')

    order.status = 'payment_review'
    order.payment_status = 'review'
    order.payment_reported_at = datetime.now()
    order.add()
    return True


async def confirm_payment(order: Order, admin_id: int) -> bool:
    if order.payment_status == 'paid':
        return False
    if order.status != 'payment_review':
        raise ValueError('Сначала пользователь должен сообщить об оплате')

    items = await OrderItem.get_for_order(order.id)
    pricing = calculate_pricing(
        [
            PricingLine(
                quantity=item.quantity,
                retail_price=item.retail_price_snapshot,
                sale_price=item.sale_price_snapshot,
                wholesale_price=item.wholesale_price_snapshot,
                owner=item.owner_snapshot,
                owner_share_percent=item.owner_share_percent_snapshot,
            )
            for item in items
        ],
        personal_percent=order.personal_discount_percent,
        promo_percent=order.promo_discount_percent,
        coins_requested=order.coins_used,
    )

    order.product_discount_total = pricing.product_discount
    order.subtotal = pricing.retail_subtotal
    order.paid_total = pricing.paid_total
    order.wholesale_total = pricing.wholesale_total
    order.net_profit = pricing.net_profit
    order.diana_share = pricing.diana_share
    order.bulat_share = pricing.bulat_share
    order.partner_reward = Decimal('0')
    if order.promo_code_id:
        promocode = await Promocode.get_by_id(order.promo_code_id)
        if promocode:
            order.partner_reward = money(
                order.paid_total * promocode.partner_reward_percent / Decimal('100')
            )

    order.status = 'paid'
    order.payment_status = 'paid'
    order.paid_at = datetime.now()
    order.paid_by_admin_id = admin_id
    order.add()

    for item in items:
        product = await Product.get_by_id(item.product_id)
        if product:
            product.purchases_count += item.quantity
            product.add()

    return True


async def cancel_order(order: Order, admin_id: int) -> bool:
    if order.status == 'cancelled':
        return False

    if order.payment_status == 'paid':
        raise ValueError('Оплаченный заказ нельзя отменить этой командой')

    if order.coins_used:
        user = await User.get_by_id(order.user_id)
        if user:
            user.coin_balance = money(user.coin_balance + order.coins_used)
            user.updated_at = datetime.now()
            user.add()
            CoinTransaction(
                user_id=user.id,
                order_id=order.id,
                amount=order.coins_used,
                balance_after=user.coin_balance,
                reason=f'Возврат за отменённый заказ {order.number}',
            ).add()

    order.status = 'cancelled'
    order.payment_status = 'cancelled'
    order.paid_by_admin_id = admin_id
    order.add()
    return True
