from alembic import op
import sqlalchemy as sa


revision = 'InviteeReward'
down_revision = 'PtXoM87GRn2JNCkAxJhhiw'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('socialchannel') as batch_op:
        batch_op.add_column(sa.Column(
            'invitee_coin_reward',
            sa.Numeric(scale=2),
            nullable=False,
            server_default=sa.text('0'),
        ))

    with op.batch_alter_table('referralreward') as batch_op:
        batch_op.add_column(sa.Column(
            'invitee_reward_amount',
            sa.Numeric(scale=2),
            nullable=False,
            server_default=sa.text('0'),
        ))


def downgrade() -> None:
    with op.batch_alter_table('referralreward') as batch_op:
        batch_op.drop_column('invitee_reward_amount')

    with op.batch_alter_table('socialchannel') as batch_op:
        batch_op.drop_column('invitee_coin_reward')
