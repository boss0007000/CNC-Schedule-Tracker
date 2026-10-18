"""machine-specific training authorization for trainers

Revision ID: 0011
Revises: 0010
"""
import app.models
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trainer_authorizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("staff_id", sa.Integer(), sa.ForeignKey("staff_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("machine_id", sa.Integer(), sa.ForeignKey("machines.id", ondelete="CASCADE"), nullable=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("machine_groups.id", ondelete="CASCADE"), nullable=True),
        sa.Column("granted_by", sa.String(length=60), nullable=False),
        sa.Column("granted_at", app.models.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint("(machine_id is null) <> (group_id is null)", name="ck_trainer_auth_target"),
        sa.UniqueConstraint("staff_id", "machine_id", name="uq_trainer_auth_machine"),
        sa.UniqueConstraint("staff_id", "group_id", name="uq_trainer_auth_group"),
    )
    op.create_index("ix_trainer_authorizations_staff_id", "trainer_authorizations", ["staff_id"])


def downgrade() -> None:
    op.drop_index("ix_trainer_authorizations_staff_id", table_name="trainer_authorizations")
    op.drop_table("trainer_authorizations")
