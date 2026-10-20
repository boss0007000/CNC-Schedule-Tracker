"""optional Reply-To on queued emails

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("email_outbox", schema=None) as b:
        b.add_column(sa.Column("reply_to", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("email_outbox", schema=None) as b:
        b.drop_column("reply_to")
