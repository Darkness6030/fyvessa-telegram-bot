import sqlalchemy as sa
from alembic import op

revision = 'OrderDelivery'
down_revision = 'RmvCatImage'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index('ix_product_is_recommended', table_name='product')
    with op.batch_alter_table('product') as batch_op:
        batch_op.alter_column('is_recommended', new_column_name='is_new')
    op.create_index('ix_product_is_new', 'product', ['is_new'], unique=False)

    with op.batch_alter_table('order') as batch_op:
        batch_op.add_column(sa.Column('shipping_status', sa.String(), nullable=False, server_default='created'))
        batch_op.add_column(sa.Column('recipient_first_name', sa.String(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('recipient_last_name', sa.String(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('recipient_phone_number', sa.String(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('delivery_method', sa.String(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('pickup_point_address', sa.Text(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('shipped_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('delivered_at', sa.DateTime(), nullable=True))
        batch_op.create_index('ix_order_shipping_status', ['shipping_status'], unique=False)
        batch_op.create_index('ix_order_shipped_at', ['shipped_at'], unique=False)
        batch_op.create_index('ix_order_delivered_at', ['delivered_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('order') as batch_op:
        batch_op.drop_index('ix_order_delivered_at')
        batch_op.drop_index('ix_order_shipped_at')
        batch_op.drop_index('ix_order_shipping_status')
        batch_op.drop_column('delivered_at')
        batch_op.drop_column('shipped_at')
        batch_op.drop_column('pickup_point_address')
        batch_op.drop_column('delivery_method')
        batch_op.drop_column('recipient_phone_number')
        batch_op.drop_column('recipient_last_name')
        batch_op.drop_column('recipient_first_name')
        batch_op.drop_column('shipping_status')

    op.drop_index('ix_product_is_new', table_name='product')
    with op.batch_alter_table('product') as batch_op:
        batch_op.alter_column('is_new', new_column_name='is_recommended')
    op.create_index('ix_product_is_recommended', 'product', ['is_recommended'], unique=False)
