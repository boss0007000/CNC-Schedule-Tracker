"""staff must change an emailed temporary password on first login

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("staff_users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("must_change_password", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("staff_users", schema=None) as batch_op:
        batch_op.drop_column("must_change_password")
