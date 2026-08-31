"""add Discord user and guild member identity

Revision ID: a8d3e5f7b912
Revises: 7c2d9a4e6f10
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8d3e5f7b912"
down_revision: str | None = "7c2d9a4e6f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("discord_users", sa.Column("username", sa.Text(), nullable=True))
    op.add_column(
        "discord_users",
        sa.Column("global_name", sa.Text(), nullable=True),
    )
    op.add_column(
        "guild_members",
        sa.Column("nickname", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("guild_members", "nickname")
    op.drop_column("discord_users", "global_name")
    op.drop_column("discord_users", "username")
