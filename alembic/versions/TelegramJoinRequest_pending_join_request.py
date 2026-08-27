import sqlalchemy as sa
from alembic import op

revision = 'TelegramJoinRequest'
down_revision = 'RemoveSocialChannelPlatform'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'telegramjoinrequest',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('social_channel_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('requested_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['social_channel_id'],
            ['socialchannel.id'],
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_telegramjoinrequest_social_channel_id',
        'telegramjoinrequest',
        ['social_channel_id'],
    )
    op.create_index(
        'ix_telegramjoinrequest_user_id',
        'telegramjoinrequest',
        ['user_id'],
    )
    op.create_index(
        'uq_telegramjoinrequest_channel_user',
        'telegramjoinrequest',
        ['social_channel_id', 'user_id'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        'uq_telegramjoinrequest_channel_user',
        table_name='telegramjoinrequest',
    )
    op.drop_index(
        'ix_telegramjoinrequest_user_id',
        table_name='telegramjoinrequest',
    )
    op.drop_index(
        'ix_telegramjoinrequest_social_channel_id',
        table_name='telegramjoinrequest',
    )
    op.drop_table('telegramjoinrequest')
