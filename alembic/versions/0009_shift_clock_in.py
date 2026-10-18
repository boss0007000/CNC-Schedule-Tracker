"""supervisors clock in to their scheduled shifts

Revision ID: 0009
Revises: 0008
"""
import app.models
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("shift_schedule", schema=None) as b:
        b.add_column(sa.Column("checked_in_at", app.models.UTCDateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("checked_out_at", app.models.UTCDateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("alerted_at", app.models.UTCDateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("shift_schedule", schema=None) as b:
        b.drop_column("alerted_at")
        b.drop_column("checked_out_at")
        b.drop_column("checked_in_at")
