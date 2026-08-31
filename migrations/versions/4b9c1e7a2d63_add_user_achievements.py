"""add user achievements

Revision ID: 4b9c1e7a2d63
Revises: d7a4e2c91b56
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4b9c1e7a2d63"
down_revision: str | None = "d7a4e2c91b56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_achievements",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("achievement_key", sa.Text(), nullable=False),
        sa.Column("unlocked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "guild_id > 0",
            name="ck_user_achievements_guild_id_positive",
        ),
        sa.CheckConstraint(
            "user_id > 0",
            name="ck_user_achievements_user_id_positive",
        ),
        sa.CheckConstraint(
            "char_length(achievement_key) BETWEEN 1 AND 128",
            name="ck_user_achievements_key_length",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "user_id"],
            ["guild_members.guild_id", "guild_members.user_id"],
            name="fk_user_achievements_guild_member",
        ),
        sa.PrimaryKeyConstraint(
            "guild_id",
            "user_id",
            "achievement_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("user_achievements")
