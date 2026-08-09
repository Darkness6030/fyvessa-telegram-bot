from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Self

from rewire import simple_plugin
from rewire_sqlmodel import SQLModel, session_context
from sqlalchemy import BigInteger, Text
from sqlmodel import Field

plugin = simple_plugin()


class Category(SQLModel, table=True):
    id: int = Field(primary_key=True)
    name: str = Field(index=True)
    image_url: Optional[str] = None
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class Product(SQLModel, table=True):
    id: int = Field(primary_key=True)
    sku: str = Field(index=True)
    category_id: int = Field(foreign_key='category.id', index=True)
    name: str = Field(index=True)
    description: str = Field(default='', sa_type=Text)
    characteristics: str = Field(default='', sa_type=Text)
    retail_price: Decimal = Field(max_digits=12, decimal_places=2)
    wholesale_price: Decimal = Field(max_digits=12, decimal_places=2)
    discount_price: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)
    image_url: Optional[str] = None
    is_active: bool = Field(default=True, index=True)
    is_popular: bool = Field(default=False, index=True)
    is_recommended: bool = Field(default=False, index=True)
    owner: str = 'Булат'
    owner_share_percent: Decimal = Field(default=Decimal('70'), max_digits=5, decimal_places=2)
    views_count: int = 0
    cart_additions_count: int = 0
    purchases_count: int = 0
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    @property
    def current_price(self) -> Decimal:
        return self.discount_price or self.retail_price


class Customer(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    user_id: int = Field(sa_type=BigInteger, index=True)
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    birth_date: Optional[date] = None
    phone_number: Optional[str] = None

    coin_balance: Decimal = Field(default=Decimal(0), decimal_places=2)
    personal_discount_percent: Decimal = Field(default=Decimal(0), decimal_places=2)
    referral_code: Optional[str] = Field(default=None, index=True)

    @property
    def is_registered(self) -> bool:
        return all((self.first_name, self.last_name, self.birth_date, self.phone_number))

    @classmethod
    async def get_or_create(
        cls,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
    ) -> Self:
        customer = await cls.select().filter_by(user_id=user_id).first()
        if not customer:
            customer = cls(
                user_id=user_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                referral_code=f'FY{user_id:X}',
            ).add()
            await session_context.get().flush()
            return customer

        customer.username = username
        customer.updated_at = datetime.now()
        return customer.add()


class Favorite(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    customer_id: int = Field(foreign_key='customer.id', index=True, ondelete='CASCADE')
    product_id: int = Field(foreign_key='product.id', index=True, ondelete='CASCADE')


class ProductView(SQLModel, table=True):
    id: int = Field(primary_key=True)
    viewed_at: datetime = Field(default_factory=datetime.now, index=True)
    customer_id: int = Field(foreign_key='customer.id', index=True, ondelete='CASCADE')
    product_id: int = Field(foreign_key='product.id', index=True, ondelete='CASCADE')


class CartItem(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    customer_id: int = Field(foreign_key='customer.id', index=True, ondelete='CASCADE')
    product_id: int = Field(foreign_key='product.id', index=True, ondelete='CASCADE')
    quantity: int = Field(default=1, ge=1, le=999)


class AvailabilityRequest(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    status: str = Field(default='pending', index=True)
    customer_id: int = Field(foreign_key='customer.id', index=True)
    product_id: int = Field(foreign_key='product.id', index=True)
    requested_quantity: Optional[int] = Field(default=None, ge=1)
    available_quantity: Optional[int] = Field(default=None, ge=0)
    admin_id: Optional[int] = Field(default=None, sa_type=BigInteger)
    admin_comment: Optional[str] = None
    resolved_at: Optional[datetime] = None


class PromoCode(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    code: str = Field(index=True)
    partner_name: str
    customer_discount_percent: Decimal = Field(
        default=Decimal('10'), max_digits=5, decimal_places=2
    )
    partner_reward_percent: Decimal = Field(
        default=Decimal('10'), max_digits=5, decimal_places=2
    )
    is_active: bool = Field(default=True, index=True)


class Order(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now, index=True)
    number: str = Field(index=True)
    customer_id: int = Field(foreign_key='customer.id', index=True)
    status: str = Field(default='draft', index=True)
    payment_status: str = Field(default='not_paid', index=True)
    promo_code_id: Optional[int] = Field(default=None, foreign_key='promocode.id')
    discount_mode: str = 'none'
    product_discount_total: Decimal = Field(default=Decimal('0'), max_digits=12, decimal_places=2)
    personal_discount_percent: Decimal = Field(default=Decimal('0'), max_digits=5, decimal_places=2)
    promo_discount_percent: Decimal = Field(default=Decimal('0'), max_digits=5, decimal_places=2)
    coins_used: Decimal = Field(default=Decimal('0'), max_digits=12, decimal_places=2)
    subtotal: Decimal = Field(default=Decimal('0'), max_digits=12, decimal_places=2)
    paid_total: Decimal = Field(default=Decimal('0'), max_digits=12, decimal_places=2)
    wholesale_total: Decimal = Field(default=Decimal('0'), max_digits=12, decimal_places=2)
    net_profit: Decimal = Field(default=Decimal('0'), max_digits=12, decimal_places=2)
    diana_share: Decimal = Field(default=Decimal('0'), max_digits=12, decimal_places=2)
    bulat_share: Decimal = Field(default=Decimal('0'), max_digits=12, decimal_places=2)
    partner_reward: Decimal = Field(default=Decimal('0'), max_digits=12, decimal_places=2)

    payment_reported_at: Optional[datetime] = None
    paid_at: Optional[datetime] = Field(default=None, index=True)
    paid_by_admin_id: Optional[int] = Field(default=None, sa_type=BigInteger)


class OrderItem(SQLModel, table=True):
    id: int = Field(primary_key=True)
    order_id: int = Field(foreign_key='order.id', index=True, ondelete='CASCADE')
    product_id: int = Field(foreign_key='product.id', index=True)
    quantity: int = Field(ge=1)
    sku_snapshot: str
    product_name_snapshot: str
    category_snapshot: str
    retail_price_snapshot: Decimal = Field(max_digits=12, decimal_places=2)
    sale_price_snapshot: Decimal = Field(max_digits=12, decimal_places=2)
    wholesale_price_snapshot: Decimal = Field(max_digits=12, decimal_places=2)
    owner_snapshot: str
    owner_share_percent_snapshot: Decimal = Field(max_digits=5, decimal_places=2)


class CoinTransaction(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now, index=True)
    customer_id: int = Field(foreign_key='customer.id', index=True)
    order_id: Optional[int] = Field(default=None, foreign_key='order.id', index=True)
    amount: Decimal = Field(max_digits=12, decimal_places=2)
    balance_after: Decimal = Field(max_digits=12, decimal_places=2)
    reason: str


class Referral(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    inviter_id: int = Field(foreign_key='customer.id', index=True)
    invitee_id: int = Field(foreign_key='customer.id', index=True)
    subscription_confirmed: bool = False
    reward_granted: bool = False
    confirmed_at: Optional[datetime] = None


class Review(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    customer_id: int = Field(foreign_key='customer.id', index=True)
    product_id: Optional[int] = Field(default=None, foreign_key='product.id', index=True)
    order_id: Optional[int] = Field(default=None, foreign_key='order.id', index=True)
    rating: int = Field(ge=1, le=5)
    text: str = Field(sa_type=Text)
    is_published: bool = Field(default=False, index=True)


class SupportRequest(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    customer_id: int = Field(foreign_key='customer.id', index=True)
    status: str = Field(default='open', index=True)
    subject: str
    message: str = Field(sa_type=Text)
    closed_at: Optional[datetime] = None


class Payout(SQLModel, table=True):
    id: int = Field(primary_key=True)
    paid_at: datetime = Field(default_factory=datetime.now, index=True)
    recipient_type: str = Field(index=True)
    recipient_name: str = Field(index=True)
    amount: Decimal = Field(max_digits=12, decimal_places=2)
    note: Optional[str] = None
    recorded_by_admin_id: int = Field(sa_type=BigInteger)
