"""add web admin access grants

Revision ID: 8d44cacc791e
Revises: a8d3e5f7b912
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d44cacc791e"
down_revision: str | None = "a8d3e5f7b912"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "web_admin_access_grants",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("granted_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("revoked_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "guild_id > 0",
            name="ck_web_admin_access_grants_guild_id_positive",
        ),
        sa.CheckConstraint(
            "user_id > 0",
            name="ck_web_admin_access_grants_user_id_positive",
        ),
        sa.CheckConstraint(
            "granted_by_user_id > 0",
            name="ck_web_admin_access_grants_granted_by_positive",
        ),
        sa.CheckConstraint(
            "revoked_by_user_id IS NULL OR revoked_by_user_id > 0",
            name="ck_web_admin_access_grants_revoked_by_positive",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by_user_id IS NULL)",
            name="ck_web_admin_access_grants_revocation_pair",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_web_admin_access_grants_revoked_after_granted",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_web_admin_access_grants_active",
        "web_admin_access_grants",
        ["guild_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_web_admin_access_grants_guild_granted_at",
        "web_admin_access_grants",
        ["guild_id", "granted_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_web_admin_access_grants_guild_granted_at",
        table_name="web_admin_access_grants",
    )
    op.drop_index(
        "uq_web_admin_access_grants_active",
        table_name="web_admin_access_grants",
    )
    op.drop_table("web_admin_access_grants")
