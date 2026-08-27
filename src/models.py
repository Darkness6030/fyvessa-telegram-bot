from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Self, Sequence

from rewire import simple_plugin
from rewire_sqlmodel import session_context, SQLModel
from sqlalchemy import BigInteger, func, Index, Text
from sqlmodel import col, Field, select

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
    is_new: bool = Field(default=False, index=True)
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
    referral_discount_awarded_at: Optional[datetime] = None
    referral_activation_reward_awarded_at: Optional[datetime] = None
    referral_activation_reward_amount: Decimal = Field(
        default=Decimal('0'),
        decimal_places=2,
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
    async def get_by_id_for_update(cls, user_id: int) -> Optional[Self]:
        return await cls.select().filter_by(id=user_id).with_for_update().first()

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

    @classmethod
    async def get_all(cls) -> list[Self]:
        return list(await cls.select().all())


class Order(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now, index=True)
    number: str = Field(index=True)
    user_id: int = Field(sa_type=BigInteger, foreign_key='user.id', index=True)
    status: str = Field(default='draft', index=True)
    payment_status: str = Field(default='not_paid', index=True)
    shipping_status: str = Field(default='created', index=True)
    recipient_first_name: str = ''
    recipient_last_name: str = ''
    recipient_phone_number: str = ''
    delivery_method: str = ''
    pickup_point_address: str = Field(default='', sa_type=Text)
    promo_code_id: Optional[int] = Field(default=None, foreign_key='promocode.id')
    partner_payout_id: Optional[int] = Field(
        default=None,
        foreign_key='partnerpayout.id',
        index=True,
    )
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
    purchase_coin_percent: Decimal = Field(default=Decimal('0'), decimal_places=2)
    purchase_coins_awarded: Decimal = Field(default=Decimal('0'), decimal_places=2)

    payment_reported_at: Optional[datetime] = None
    paid_at: Optional[datetime] = Field(default=None, index=True)
    paid_by_admin_id: Optional[int] = Field(default=None, sa_type=BigInteger)
    shipped_at: Optional[datetime] = Field(default=None, index=True)
    delivered_at: Optional[datetime] = Field(default=None, index=True)

    @classmethod
    async def get_by_id(cls, order_id: int, user_id: Optional[int] = None) -> Optional[Self]:
        query = cls.select().filter_by(id=order_id)
        if user_id is not None:
            query = query.filter_by(user_id=user_id)

        return await query.first()

    @classmethod
    async def get_by_id_for_update(cls, order_id: int) -> Optional[Self]:
        return await cls.select().filter_by(id=order_id).with_for_update().first()

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
    social_channel_id: Optional[int] = Field(
        default=None,
        foreign_key='socialchannel.id',
        index=True,
    )
    referral_reward_id: Optional[int] = Field(
        default=None,
        foreign_key='referralreward.id',
        index=True,
    )
    admin_id: Optional[int] = Field(default=None, sa_type=BigInteger, index=True)
    amount: Decimal = Field(decimal_places=2)
    balance_after: Decimal = Field(decimal_places=2)
    reason: str

    @property
    def display_reason(self) -> str:
        """Hide the obsolete prefix in old referral transaction texts."""
        if not self.social_channel_id:
            return self.reason

        prefix, separator, details = self.reason.partition(': ')
        is_referral_reason = (
            prefix == 'Награда за подписку'
            or prefix.startswith('Подписка приглашённого ')
        )
        if separator and is_referral_reason and ' / ' in details:
            return f'{prefix}: {details.split(" / ", 1)[1]}'
        return self.reason

    @classmethod
    async def get_recent(
        cls,
        user_id: Optional[int] = None,
        limit: int = 30,
    ) -> list[Self]:
        query = cls.select()
        if user_id is not None:
            query = query.filter_by(user_id=user_id)
        return list(await query.order_by(cls.created_at.desc()).limit(limit).all())

    @classmethod
    async def get_page(
        cls,
        page: int,
        page_size: int,
        user_id: Optional[int] = None,
    ) -> list[Self]:
        query = cls.select()
        if user_id is not None:
            query = query.filter_by(user_id=user_id)
        return list(
            await query
            .order_by(cls.created_at.desc(), cls.id.desc())
            .offset(max(page, 0) * page_size)
            .limit(page_size)
            .all()
        )

    @classmethod
    async def count(cls, user_id: Optional[int] = None) -> int:
        query = select(func.count(cls.id))
        if user_id is not None:
            query = query.where(cls.user_id == user_id)
        return (await session_context.get().execute(query)).scalar_one()


class AppSetting(SQLModel, table=True):
    __table_args__ = (Index('uq_appsetting_key', 'key', unique=True),)

    id: int = Field(primary_key=True)
    key: str = Field(index=True)
    value: str = ''
    updated_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    async def get_by_key(cls, key: str) -> Optional[Self]:
        return await cls.select().filter_by(key=key).first()


class Banner(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    title: str
    image_url: str = ''
    target_url: str = '/catalog'
    position: int = Field(default=0, index=True)
    is_active: bool = Field(default=True, index=True)

    @classmethod
    async def get_by_id(cls, banner_id: int) -> Optional[Self]:
        return await cls.select().filter_by(id=banner_id).first()

    @classmethod
    async def get_all(cls) -> list[Self]:
        return list(await cls.select().order_by(cls.position, cls.id).all())

    @classmethod
    async def get_active(cls) -> list[Self]:
        return list(
            await cls.select()
            .where(col(cls.is_active).is_(True))
            .order_by(cls.position, cls.id)
            .all()
        )


class SocialChannel(SQLModel, table=True):
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    account_name: str
    url: str
    coin_reward: Decimal = Field(default=Decimal('0'), decimal_places=2)
    invitee_coin_reward: Decimal = Field(default=Decimal('0'), decimal_places=2)
    telegram_chat_id: Optional[str] = None
    is_active: bool = Field(default=True, index=True)

    @property
    def supports_automatic_check(self) -> bool:
        return bool(self.telegram_chat_id)

    @classmethod
    async def get_by_id(cls, channel_id: int) -> Optional[Self]:
        return await cls.select().filter_by(id=channel_id).first()

    @classmethod
    async def get_all(cls) -> list[Self]:
        return list(await cls.select().order_by(cls.created_at.desc()).all())

    @classmethod
    async def get_active(cls) -> list[Self]:
        return list(
            await cls.select()
            .where(col(cls.is_active).is_(True))
            .order_by(cls.created_at, cls.id)
            .all()
        )


class TelegramJoinRequest(SQLModel, table=True):
    __table_args__ = (
        Index(
            'uq_telegramjoinrequest_channel_user',
            'social_channel_id',
            'user_id',
            unique=True,
        ),
    )

    id: int = Field(primary_key=True)
    social_channel_id: int = Field(foreign_key='socialchannel.id', index=True)
    user_id: int = Field(sa_type=BigInteger, index=True)
    requested_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    async def get_for_user_channel(
        cls,
        user_id: int,
        social_channel_id: int,
    ) -> Optional[Self]:
        return await cls.select().filter_by(
            user_id=user_id,
            social_channel_id=social_channel_id,
        ).first()


class ReferralReward(SQLModel, table=True):
    __table_args__ = (
        Index(
            'uq_referralreward_invited_channel',
            'invited_user_id',
            'social_channel_id',
            unique=True,
        ),
    )

    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now, index=True)
    invited_user_id: int = Field(
        sa_type=BigInteger,
        foreign_key='user.id',
        index=True,
    )
    referrer_id: Optional[int] = Field(
        default=None,
        sa_type=BigInteger,
        foreign_key='user.id',
        index=True,
    )
    social_channel_id: int = Field(foreign_key='socialchannel.id', index=True)
    status: str = Field(default='pending', index=True)
    reward_amount: Decimal = Field(default=Decimal('0'), decimal_places=2)
    invitee_reward_amount: Decimal = Field(default=Decimal('0'), decimal_places=2)
    verified_at: Optional[datetime] = None
    reviewed_by_admin_id: Optional[int] = Field(default=None, sa_type=BigInteger)

    @classmethod
    async def get_by_id(cls, reward_id: int) -> Optional[Self]:
        return await cls.select().filter_by(id=reward_id).first()

    @classmethod
    async def get_by_id_for_update(cls, reward_id: int) -> Optional[Self]:
        return await cls.select().filter_by(id=reward_id).with_for_update().first()

    @classmethod
    async def get_for_user_channel(
        cls,
        invited_user_id: int,
        social_channel_id: int,
    ) -> Optional[Self]:
        return await cls.select().filter_by(
            invited_user_id=invited_user_id,
            social_channel_id=social_channel_id,
        ).first()

    @classmethod
    async def get_for_invited_user(cls, invited_user_id: int) -> list[Self]:
        return list(
            await cls.select()
            .filter_by(invited_user_id=invited_user_id)
            .order_by(cls.created_at)
            .all()
        )

    @classmethod
    async def get_recent(
        cls,
        status: Optional[str] = None,
        limit: int = 30,
    ) -> list[Self]:
        query = cls.select()
        if status:
            query = query.filter_by(status=status)
        return list(await query.order_by(cls.created_at.desc()).limit(limit).all())

    @classmethod
    async def get_all(cls) -> list[Self]:
        return list(await cls.select().all())


class PartnerPayout(SQLModel, table=True):
    __table_args__ = (
        Index(
            'uq_partnerpayout_promo_period',
            'promo_code_id',
            'period_ended_at',
            unique=True,
        ),
    )

    id: int = Field(primary_key=True)
    generated_at: datetime = Field(default_factory=datetime.now, index=True)
    period_started_at: Optional[datetime] = None
    period_ended_at: datetime = Field(index=True)
    promo_code_id: Optional[int] = Field(
        default=None,
        foreign_key='promocode.id',
        index=True,
    )
    partner_name_snapshot: str
    promo_code_snapshot: str
    reward_percent_snapshot: Decimal = Field(decimal_places=2)
    orders_count: int
    orders_total: Decimal = Field(decimal_places=2)
    payout_amount: Decimal = Field(decimal_places=2)
    status: str = Field(default='pending', index=True)
    paid_at: Optional[datetime] = Field(default=None, index=True)
    paid_by_admin_id: Optional[int] = Field(default=None, sa_type=BigInteger)

    @classmethod
    async def get_by_id(cls, payout_id: int) -> Optional[Self]:
        return await cls.select().filter_by(id=payout_id).first()

    @classmethod
    async def get_by_id_for_update(cls, payout_id: int) -> Optional[Self]:
        return await cls.select().filter_by(id=payout_id).with_for_update().first()

    @classmethod
    async def get_recent(
        cls,
        status: Optional[str] = None,
        limit: Optional[int] = 30,
    ) -> list[Self]:
        query = cls.select()
        if status:
            query = query.filter_by(status=status)
        query = query.order_by(cls.generated_at.desc())
        if limit is not None:
            query = query.limit(limit)
        return list(await query.all())
