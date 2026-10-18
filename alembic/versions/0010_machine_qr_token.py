"""per-machine QR token for scan-to-check-in

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("machines", schema=None) as b:
        b.add_column(sa.Column("qr_token", sa.String(length=24), nullable=True))
        b.create_index("ix_machines_qr_token", ["qr_token"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("machines", schema=None) as b:
        b.drop_index("ix_machines_qr_token")
        b.drop_column("qr_token")
