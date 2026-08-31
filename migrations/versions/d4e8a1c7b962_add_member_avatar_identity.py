"""Add persisted global and guild-specific Discord avatar identity.

Revision ID: d4e8a1c7b962
Revises: a4f6c8d21e73
Create Date: 2026-08-30
"""

import sqlalchemy as sa
from alembic import op

revision: str = "d4e8a1c7b962"
down_revision: str | None = "a4f6c8d21e73"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "discord_users",
        sa.Column("avatar_hash", sa.Text(), nullable=True),
    )
    op.add_column(
        "guild_members",
        sa.Column("guild_avatar_hash", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("guild_members", "guild_avatar_hash")
    op.drop_column("discord_users", "avatar_hash")
