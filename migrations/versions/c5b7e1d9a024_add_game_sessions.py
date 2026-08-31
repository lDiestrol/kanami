"""Add crash-safe game sessions.

Revision ID: c5b7e1d9a024
Revises: 3e7b9c2a6f41
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5b7e1d9a024"
down_revision: str | None = "3e7b9c2a6f41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "game_sessions",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("game_key", sa.Text(), nullable=False),
        sa.Column("game_name", sa.Text(), nullable=False),
        sa.Column("application_id", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "confirmed_through_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "application_id IS NULL OR application_id > 0",
            name="ck_game_sessions_application_id_positive",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR confirmed_through_at <= ended_at",
            name="ck_game_sessions_confirmed_before_end",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_game_sessions_end_after_start",
        ),
        sa.CheckConstraint(
            "btrim(game_key) <> ''",
            name="ck_game_sessions_game_key_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(game_name) <> ''",
            name="ck_game_sessions_game_name_not_blank",
        ),
        sa.CheckConstraint(
            "started_at <= confirmed_through_at",
            name="ck_game_sessions_started_before_confirmed",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "user_id"],
            ["guild_members.guild_id", "guild_members.user_id"],
            name="fk_game_sessions_guild_member",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_game_sessions_guild_started_at",
        "game_sessions",
        ["guild_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_game_sessions_guild_user_started_at",
        "game_sessions",
        ["guild_id", "user_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "uq_game_sessions_open_guild_user",
        "game_sessions",
        ["guild_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_game_sessions_open_guild_user", table_name="game_sessions")
    op.drop_index("ix_game_sessions_guild_user_started_at", table_name="game_sessions")
    op.drop_index("ix_game_sessions_guild_started_at", table_name="game_sessions")
    op.drop_table("game_sessions")
