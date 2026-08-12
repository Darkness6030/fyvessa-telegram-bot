from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Self, Sequence

from rewire import simple_plugin
from rewire_sqlmodel import SQLModel, session_context
from sqlalchemy import BigInteger, Text, func
from sqlmodel import Field, col, select

plugin = simple_plugin()


class Category(SQLModel, table=True):
    id: int = Field(primary_key=True)
    name: str = Field(index=True)
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    async def get_by_id(cls, category_id: int) -> Optional[Self]:
        return await cls.select().filter_by(id=category_id).first()

    @classmethod
    async def get_active(cls) -> list[Self]:
        return list(
            await cls.select()
            .where(col(cls.is_active).is_(True))
            .order_by(cls.name)
            .all()
        )

    @classmethod
    async def get_all(cls) -> list[Self]:
        return list(await cls.select().all())


class Product(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    sku: str = Field(index=True)
    category_id: int = Field(foreign_key='category.id', index=True)
    name: str = Field(index=True)
    description: str = Field(default='', sa_type=Text)
    characteristics: str = Field(default='', sa_type=Text)
    retail_price: Decimal = Field(decimal_places=2)
    wholesale_price: Decimal = Field(decimal_places=2)
    discount_price: Optional[Decimal] = Field(default=None, decimal_places=2)
    image_url: Optional[str] = None
    is_active: bool = Field(default=True, index=True)
    is_popular: bool = Field(default=False, index=True)
    is_recommended: bool = Field(default=False, index=True)
    owner: str = 'Булат'
    owner_share_percent: Decimal = Field(default=Decimal('70'), decimal_places=2)
    views_count: int = 0
    cart_additions_count: int = 0
    purchases_count: int = 0

    @property
    def current_price(self) -> Decimal:
        return self.discount_price or self.retail_price

    @classmethod
    async def get_by_id(cls, product_id: int, active_only: bool = False) -> Optional[Self]:
        query = cls.select().filter_by(id=product_id)
        if active_only:
            query = query.filter_by(is_active=True)
        return await query.first()

    @classmethod
    async def get_by_sku(cls, sku: str, active_only: bool = False) -> Optional[Self]:
        query = cls.select().filter_by(sku=sku)
        if active_only:
            query = query.filter_by(is_active=True)
        return await query.first()

    @classmethod
    async def get_by_ids(cls, product_ids: Sequence[int], active_only: bool = False) -> list[Self]:
        if not product_ids:
            return []
        query = cls.select().where(col(cls.id).in_(product_ids))
        if active_only:
            query = query.where(col(cls.is_active).is_(True))
        return list(await query.all())

    @classmethod
    async def search(
        cls, q: Optional[str] = None, category_id: Optional[int] = None,
        min_price: Optional[int] = None, max_price: Optional[int] = None,
    ) -> list[Self]:
        query = cls.select().where(col(cls.is_active).is_(True))
        if q and q.strip():
            query = query.where(col(cls.name).ilike(f'%{q.strip()}%'))
        if category_id is not None:
            query = query.where(cls.category_id == category_id)
        current_price = func.coalesce(cls.discount_price, cls.retail_price)
        if min_price is not None:
            query = query.where(current_price >= min_price)
        if max_price is not None:
            query = query.where(current_price <= max_price)
        return list(await query.order_by(cls.name).all())

    @classmethod
    async def get_all(cls) -> list[Self]:
        return list(await cls.select().all())


class User(SQLModel, table=True):
    id: int = Field(sa_type=BigInteger, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    birth_date: Optional[date] = None
    phone_number: Optional[str] = None
    referrer_id: Optional[int] = Field(
        default=None,
        sa_type=BigInteger,
        foreign_key='user.id',
        index=True,
        ondelete='SET NULL',
    )

    coin_balance: Decimal = Field(default=Decimal(0), decimal_places=2)
    personal_discount_percent: Decimal = Field(default=Decimal(0), decimal_places=2)

    @property
    def is_registered(self) -> bool:
        return all((self.first_name, self.last_name, self.birth_date, self.phone_number))

    @classmethod
    async def get_by_id(cls, user_id: int) -> Optional[Self]:
        return await cls.select().filter_by(id=user_id).first()

    @classmethod
    async def find(cls, value: str) -> Optional[Self]:
        value = value.strip().lstrip('@')
        if not value:
            return None

        if value.isdigit():
            return await cls.get_by_id(int(value))

        return await cls.select().where(func.lower(cls.username) == value.lower()).first()

    @classmethod
    async def get_recent(cls, limit: int = 15) -> list[Self]:
        return list(await cls.select().order_by(cls.created_at.desc()).limit(limit).all())

    @classmethod
    async def get_all(cls) -> list[Self]:
        return list(await cls.select().all())

    @classmethod
    async def get_or_create(
        cls,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
        referrer_id: Optional[int] = None,
    ) -> Self:
        user = await cls.get_by_id(user_id)
        if not user:
            return cls(
                id=user_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                referrer_id=referrer_id,
            ).add()

        user.username = username
        user.updated_at = datetime.now()
        return user.add()


class Favorite(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    user_id: int = Field(sa_type=BigInteger, foreign_key='user.id', index=True, ondelete='CASCADE')
    product_id: int = Field(foreign_key='product.id', index=True, ondelete='CASCADE')

    @classmethod
    async def get_for_user(cls, user_id: int) -> list[Self]:
        return list(await cls.select().filter_by(user_id=user_id).all())

    @classmethod
    async def get_for_product(cls, user_id: int, product_id: int) -> Optional[Self]:
        return await cls.select().filter_by(user_id=user_id, product_id=product_id).first()


class ProductView(SQLModel, table=True):
    id: int = Field(primary_key=True)
    viewed_at: datetime = Field(default_factory=datetime.now, index=True)
    user_id: int = Field(sa_type=BigInteger, foreign_key='user.id', index=True, ondelete='CASCADE')
    product_id: int = Field(foreign_key='product.id', index=True, ondelete='CASCADE')

    @classmethod
    async def get_for_user(cls, user_id: int, limit: int = 20) -> list[Self]:
        return list(
            await cls.select()
            .filter_by(user_id=user_id)
            .order_by(cls.viewed_at.desc())
            .limit(limit)
            .all()
        )

    @classmethod
    async def get_for_product(cls, user_id: int, product_id: int) -> Optional[Self]:
        return await cls.select().filter_by(user_id=user_id, product_id=product_id).first()


class CartItem(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    user_id: int = Field(sa_type=BigInteger, foreign_key='user.id', index=True, ondelete='CASCADE')
    product_id: int = Field(foreign_key='product.id', index=True, ondelete='CASCADE')
    quantity: int = Field(default=1, ge=1, le=999)

    @classmethod
    async def get_for_user(cls, user_id: int) -> list[Self]:
        return list(await cls.select().filter_by(user_id=user_id).all())

    @classmethod
    async def get_for_product(cls, user_id: int, product_id: int) -> Optional[Self]:
        return await cls.select().filter_by(user_id=user_id, product_id=product_id).first()


class AvailabilityRequest(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    status: str = Field(default='pending', index=True)
    user_id: int = Field(sa_type=BigInteger, foreign_key='user.id', index=True)
    product_id: int = Field(foreign_key='product.id', index=True)
    requested_quantity: Optional[int] = Field(default=None, ge=1)
    available_quantity: Optional[int] = Field(default=None, ge=0)
    admin_id: Optional[int] = Field(default=None, sa_type=BigInteger)
    admin_comment: Optional[str] = None
    resolved_at: Optional[datetime] = None

    @classmethod
    async def get_by_id(cls, request_id: int) -> Optional[Self]:
        return await cls.select().filter_by(id=request_id).first()

    @classmethod
    async def get_pending(cls, user_id: int, product_id: int) -> Optional[Self]:
        return await cls.select().filter_by(user_id=user_id, product_id=product_id, status='pending').first()

    @classmethod
    async def get_recent(
        cls,
        user_id: Optional[int] = None,
        pending_only: bool = False,
        limit: Optional[int] = 15,
    ) -> list[Self]:
        query = cls.select()
        if user_id is not None:
            query = query.filter_by(user_id=user_id)

        if pending_only:
            query = query.filter_by(status='pending')

        query = query.order_by(cls.created_at.desc())
        if limit is not None:
            query = query.limit(limit)

        return list(await query.all())

    @classmethod
    async def get_latest_for_products(cls, user_id: int, product_ids: Sequence[int]) -> list[Self]:
        if not product_ids:
            return []

        return list(
            await cls.select()
            .where(cls.user_id == user_id)
            .where(col(cls.product_id).in_(product_ids))
            .order_by(cls.created_at.desc())
            .all()
        )


class Promocode(SQLModel, table=True):
    __tablename__ = 'promocode'

    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    code: str = Field(index=True)
    partner_name: str
    user_discount_percent: Decimal = Field(default=Decimal('10'), decimal_places=2)
    partner_reward_percent: Decimal = Field(default=Decimal('10'), decimal_places=2)
    is_active: bool = Field(default=True, index=True)
    is_deleted: bool = Field(default=False, index=True)

    def toggle(self) -> Self:
        self.is_active = not self.is_active
        return self.add()

    def mark_deleted(self) -> Self:
        self.is_active = False
        self.is_deleted = True
        return self.add()

    @classmethod
    async def get_by_id(cls, promocode_id: int) -> Optional[Self]:
        return await cls.select().filter_by(id=promocode_id).first()

    @classmethod
    async def get_by_code(cls, code: str, active_only: bool = False) -> Optional[Self]:
        query = (
            cls.select()
            .where(func.upper(cls.code) == code.strip().upper())
            .where(col(cls.is_deleted).is_(False))
        )

        if active_only:
            query = query.where(col(cls.is_active).is_(True))

        return await query.first()

    @classmethod
    async def get_recent(cls, limit: int = 15) -> list[Self]:
        return list(
            await cls.select()
            .where(col(cls.is_deleted).is_(False))
            .order_by(cls.created_at.desc())
            .limit(limit)
            .all()
        )


class Order(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now, index=True)
    number: str = Field(index=True)
    user_id: int = Field(sa_type=BigInteger, foreign_key='user.id', index=True)
    status: str = Field(default='draft', index=True)
    payment_status: str = Field(default='not_paid', index=True)
    promo_code_id: Optional[int] = Field(default=None, foreign_key='promocode.id')
    discount_mode: str = 'none'
    product_discount_total: Decimal = Field(default=Decimal('0'), decimal_places=2)
    personal_discount_percent: Decimal = Field(default=Decimal('0'), decimal_places=2)
    promo_discount_percent: Decimal = Field(default=Decimal('0'), decimal_places=2)
    coins_used: Decimal = Field(default=Decimal('0'), decimal_places=2)
    subtotal: Decimal = Field(default=Decimal('0'), decimal_places=2)
    paid_total: Decimal = Field(default=Decimal('0'), decimal_places=2)
    wholesale_total: Decimal = Field(default=Decimal('0'), decimal_places=2)
    net_profit: Decimal = Field(default=Decimal('0'), decimal_places=2)
    diana_share: Decimal = Field(default=Decimal('0'), decimal_places=2)
    bulat_share: Decimal = Field(default=Decimal('0'), decimal_places=2)
    partner_reward: Decimal = Field(default=Decimal('0'), decimal_places=2)

    payment_reported_at: Optional[datetime] = None
    paid_at: Optional[datetime] = Field(default=None, index=True)
    paid_by_admin_id: Optional[int] = Field(default=None, sa_type=BigInteger)

    @classmethod
    async def get_by_id(cls, order_id: int, user_id: Optional[int] = None) -> Optional[Self]:
        query = cls.select().filter_by(id=order_id)
        if user_id is not None:
            query = query.filter_by(user_id=user_id)

        return await query.first()

    @classmethod
    async def get_recent(cls, user_id: Optional[int] = None, limit: Optional[int] = 15) -> list[Self]:
        query = cls.select()
        if user_id is not None:
            query = query.filter_by(user_id=user_id)

        query = query.order_by(cls.created_at.desc())
        if limit is not None:
            query = query.limit(limit)

        return list(await query.all())

    @classmethod
    async def get_all(cls) -> list[Self]:
        return list(await cls.select().all())

    @classmethod
    async def count_for_promocode(cls, promocode_id: int) -> int:
        query = (
            select(func.count(cls.id))
            .where(cls.promo_code_id == promocode_id)
            .where(cls.status != 'cancelled')
        )
        return (await session_context.get().execute(query)).scalar_one()


class OrderItem(SQLModel, table=True):
    id: int = Field(primary_key=True)
    order_id: int = Field(foreign_key='order.id', index=True, ondelete='CASCADE')
    product_id: int = Field(foreign_key='product.id', index=True)
    quantity: int = Field(ge=1)
    sku_snapshot: str
    product_name_snapshot: str
    category_snapshot: str
    retail_price_snapshot: Decimal = Field(decimal_places=2)
    sale_price_snapshot: Decimal = Field(decimal_places=2)
    wholesale_price_snapshot: Decimal = Field(decimal_places=2)
    owner_snapshot: str
    owner_share_percent_snapshot: Decimal = Field(decimal_places=2)

    @classmethod
    async def get_for_order(cls, order_id: int) -> list[Self]:
        return list(await cls.select().filter_by(order_id=order_id).all())


class CoinTransaction(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now, index=True)
    user_id: int = Field(sa_type=BigInteger, foreign_key='user.id', index=True)
    order_id: Optional[int] = Field(default=None, foreign_key='order.id', index=True)
    amount: Decimal = Field(decimal_places=2)
    balance_after: Decimal = Field(decimal_places=2)
    reason: str
