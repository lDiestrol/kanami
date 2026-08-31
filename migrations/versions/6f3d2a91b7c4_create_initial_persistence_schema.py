"""Create initial persistence schema.

Revision ID: 6f3d2a91b7c4
Revises:
Create Date: 2026-08-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6f3d2a91b7c4"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the initial persistence schema."""

    op.create_table(
        "guilds",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.CheckConstraint("id > 0", name="ck_guilds_id_positive"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "discord_users",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("is_bot", sa.Boolean(), nullable=False),
        sa.CheckConstraint("id > 0", name="ck_discord_users_id_positive"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "guild_members",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "left_at IS NULL OR joined_at IS NULL OR left_at >= joined_at",
            name="ck_guild_members_membership_time_order",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id"],
            ["guilds.id"],
            name="fk_guild_members_guild_id_guilds",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["discord_users.id"],
            name="fk_guild_members_user_id_discord_users",
        ),
        sa.PrimaryKeyConstraint("guild_id", "user_id"),
    )
    op.create_table(
        "voice_channels",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("channel_kind", sa.Text(), nullable=False),
        sa.Column("is_afk", sa.Boolean(), nullable=False),
        sa.CheckConstraint("id > 0", name="ck_voice_channels_id_positive"),
        sa.CheckConstraint(
            "channel_kind IN ('voice', 'stage')",
            name="ck_voice_channels_channel_kind",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id"],
            ["guilds.id"],
            name="fk_voice_channels_guild_id_guilds",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id",
            "id",
            name="uq_voice_channels_guild_id_id",
        ),
    )
    op.create_table(
        "voice_sessions",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "confirmed_through_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "started_at <= confirmed_through_at",
            name="ck_voice_sessions_started_before_confirmed",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_voice_sessions_end_after_start",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR confirmed_through_at <= ended_at",
            name="ck_voice_sessions_confirmed_before_end",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "user_id"],
            ["guild_members.guild_id", "guild_members.user_id"],
            name="fk_voice_sessions_guild_member",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "guild_id",
            "user_id",
            name="uq_voice_sessions_id_guild_id_user_id",
        ),
    )
    op.create_index(
        "ix_voice_sessions_guild_user_started_at",
        "voice_sessions",
        ["guild_id", "user_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "uq_voice_sessions_open_guild_user",
        "voice_sessions",
        ["guild_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_table(
        "voice_intervals",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quality", sa.Text(), nullable=False),
        sa.Column("channel_kind", sa.Text(), nullable=False),
        sa.Column("is_afk", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_voice_intervals_end_after_start",
        ),
        sa.CheckConstraint(
            "quality IN ('exact', 'estimated')",
            name="ck_voice_intervals_quality",
        ),
        sa.CheckConstraint(
            "channel_kind IN ('voice', 'stage')",
            name="ck_voice_intervals_channel_kind",
        ),
        sa.CheckConstraint(
            "ended_at IS NOT NULL OR quality = 'exact'",
            name="ck_voice_intervals_open_must_be_exact",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "channel_id"],
            ["voice_channels.guild_id", "voice_channels.id"],
            name="fk_voice_intervals_guild_channel",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "guild_id", "user_id"],
            [
                "voice_sessions.id",
                "voice_sessions.guild_id",
                "voice_sessions.user_id",
            ],
            name="fk_voice_intervals_session_guild_user",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_voice_intervals_guild_channel_started_at",
        "voice_intervals",
        ["guild_id", "channel_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_voice_intervals_guild_started_at",
        "voice_intervals",
        ["guild_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_voice_intervals_guild_user_started_at",
        "voice_intervals",
        ["guild_id", "user_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_voice_intervals_session_started_at",
        "voice_intervals",
        ["session_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "uq_voice_intervals_open_guild_user",
        "voice_intervals",
        ["guild_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    """Drop the initial persistence schema."""

    op.drop_index(
        "uq_voice_intervals_open_guild_user",
        table_name="voice_intervals",
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.drop_index(
        "ix_voice_intervals_session_started_at",
        table_name="voice_intervals",
    )
    op.drop_index(
        "ix_voice_intervals_guild_user_started_at",
        table_name="voice_intervals",
    )
    op.drop_index(
        "ix_voice_intervals_guild_started_at",
        table_name="voice_intervals",
    )
    op.drop_index(
        "ix_voice_intervals_guild_channel_started_at",
        table_name="voice_intervals",
    )
    op.drop_table("voice_intervals")
    op.drop_index(
        "uq_voice_sessions_open_guild_user",
        table_name="voice_sessions",
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.drop_index(
        "ix_voice_sessions_guild_user_started_at",
        table_name="voice_sessions",
    )
    op.drop_table("voice_sessions")
    op.drop_table("voice_channels")
    op.drop_table("guild_members")
    op.drop_table("discord_users")
    op.drop_table("guilds")
