"""staff roles

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa
import app.models


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The old CHECK limited roles to admin/supervisor/monitor; roles are now richer (see services/staffauth.py).
    with op.batch_alter_table('staff_users', schema=None) as batch_op:
        batch_op.drop_constraint('ck_staff_role', type_='check')
        batch_op.add_column(sa.Column('roles', sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
        batch_op.alter_column('role', existing_type=sa.VARCHAR(length=20), type_=sa.String(length=30), existing_nullable=False)

    # Backfill: everyone keeps exactly the role they had (supervisor = "Lead supervisor", same powers as before).
    staff = sa.table("staff_users", sa.column("id", sa.Integer), sa.column("role", sa.String), sa.column("roles", sa.JSON))
    conn = op.get_bind()
    for sid, role in conn.execute(sa.select(staff.c.id, staff.c.role)).fetchall():
        conn.execute(staff.update().where(staff.c.id == sid).values(roles=[role]))


def downgrade() -> None:
    staff = sa.table("staff_users", sa.column("role", sa.String))
    op.execute(staff.update().where(staff.c.role.not_in(["admin", "supervisor", "monitor"])).values(role="supervisor"))
    with op.batch_alter_table('staff_users', schema=None) as batch_op:
        batch_op.alter_column('role', existing_type=sa.String(length=30), type_=sa.VARCHAR(length=20), existing_nullable=False)
        batch_op.drop_column('roles')
        batch_op.create_check_constraint('ck_staff_role', "role in ('admin','supervisor','monitor')")
