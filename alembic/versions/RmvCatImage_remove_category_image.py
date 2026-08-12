from alembic import op
from sqlalchemy import Column
from sqlmodel.sql.sqltypes import AutoString


revision = 'RmvCatImage'
down_revision = 'GlTRYSMPcQdm6Ti52WfR0Lg'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('category', 'image_url')


def downgrade() -> None:
    op.add_column('category', Column('image_url', AutoString(), nullable=True))
