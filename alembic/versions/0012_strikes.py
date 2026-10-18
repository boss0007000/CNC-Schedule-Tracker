"""strikes: justified, expiring, voidable; three active strikes trigger a time-limited suspension

Revision ID: 0012
Revises: 0011
"""
import app.models
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strikes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("students.id"), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("reservation_id", sa.Integer(), sa.ForeignKey("reservations.id"), nullable=True),
        sa.Column("issued_by", sa.String(length=80), nullable=False),
        sa.Column("issued_at", app.models.UTCDateTime(timezone=True), nullable=False),
        sa.Column("voided_at", app.models.UTCDateTime(timezone=True), nullable=True),
        sa.Column("voided_by", sa.String(length=80), nullable=True),
        sa.Column("void_reason", sa.String(length=500), nullable=True),
    )
    op.create_index("ix_strikes_student_id", "strikes", ["student_id"])


def downgrade() -> None:
    op.drop_index("ix_strikes_student_id", table_name="strikes")
    op.drop_table("strikes")
