from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

MONEY = Decimal('0.01')


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PricingLine:
    quantity: int
    retail_price: Decimal
    sale_price: Decimal
    wholesale_price: Decimal
    owner: str
    owner_share_percent: Decimal


@dataclass(frozen=True)
class PricingResult:
    retail_subtotal: Decimal
    sale_subtotal: Decimal
    product_discount: Decimal
    order_discount: Decimal
    coins_used: Decimal
    paid_total: Decimal
    wholesale_total: Decimal
    net_profit: Decimal
    diana_share: Decimal
    bulat_share: Decimal


def calculate_pricing(
    lines: list[PricingLine],
    personal_discount_percent: Decimal = Decimal('0'),
    promo_discount_percent: Decimal = Decimal('0'),
    coins_requested: Decimal = Decimal('0'),
) -> PricingResult:
    if not lines or any(line.quantity < 1 for line in lines):
        raise ValueError('At least one positive-quantity line is required')

    if personal_discount_percent and promo_discount_percent:
        raise ValueError('Personal discount and promo code cannot be combined')

    discount_percent = personal_discount_percent or promo_discount_percent
    if not Decimal('0') <= discount_percent <= Decimal('100'):
        raise ValueError('Discount percent must be between 0 and 100')

    if coins_requested < 0:
        raise ValueError('Coins cannot be negative')

    retail_subtotal = money(
        sum(line.retail_price * line.quantity for line in lines)
    )

    sale_subtotal = money(sum(line.sale_price * line.quantity for line in lines))
    wholesale_total = money(
        sum(line.wholesale_price * line.quantity for line in lines)
    )

    product_discount = money(retail_subtotal - sale_subtotal)
    order_discount = money(sale_subtotal * discount_percent / Decimal('100'))
    after_discount = money(sale_subtotal - order_discount)
    coins_used = money(min(coins_requested, after_discount))
    paid_total = money(after_discount - coins_used)
    net_profit = money(paid_total - wholesale_total)

    diana_share = Decimal('0')
    bulat_share = Decimal('0')
    allocated_paid = Decimal('0')

    for index, line in enumerate(lines):
        line_sale = money(line.sale_price * line.quantity)
        if index == len(lines) - 1:
            line_paid = money(paid_total - allocated_paid)
        else:
            line_paid = money(paid_total * line_sale / sale_subtotal)
            allocated_paid += line_paid

        line_cost = money(line.wholesale_price * line.quantity)
        line_profit = money(line_paid - line_cost)

        owner_share = money(line_profit * line.owner_share_percent / Decimal('100'))
        other_share = money(line_profit - owner_share)
        if line.owner.casefold() == 'диана':
            diana_share += owner_share
            bulat_share += other_share
        else:
            bulat_share += owner_share
            diana_share += other_share

    diana_share = money(diana_share)
    bulat_share = money(net_profit - diana_share)

    return PricingResult(
        retail_subtotal=retail_subtotal,
        sale_subtotal=sale_subtotal,
        product_discount=product_discount,
        order_discount=order_discount,
        coins_used=coins_used,
        paid_total=paid_total,
        wholesale_total=wholesale_total,
        net_profit=net_profit,
        diana_share=diana_share,
        bulat_share=bulat_share,
    )
