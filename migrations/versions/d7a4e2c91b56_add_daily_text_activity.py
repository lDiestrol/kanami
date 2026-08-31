"""add daily text activity aggregates

Revision ID: d7a4e2c91b56
Revises: 91c4f28a6d3e
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7a4e2c91b56"
down_revision: str | None = "91c4f28a6d3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_text_activity",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("activity_date", sa.Date(), nullable=False),
        sa.Column("message_count", sa.BigInteger(), nullable=False),
        sa.Column("attachment_count", sa.BigInteger(), nullable=False),
        sa.Column("reply_count", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "attachment_count >= 0",
            name="ck_daily_text_activity_attachment_count_nonnegative",
        ),
        sa.CheckConstraint(
            "channel_id > 0",
            name="ck_daily_text_activity_channel_id_positive",
        ),
        sa.CheckConstraint(
            "message_count >= 0",
            name="ck_daily_text_activity_message_count_nonnegative",
        ),
        sa.CheckConstraint(
            "reply_count >= 0",
            name="ck_daily_text_activity_reply_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "user_id"],
            ["guild_members.guild_id", "guild_members.user_id"],
            name="fk_daily_text_activity_guild_member",
        ),
        sa.PrimaryKeyConstraint(
            "guild_id",
            "user_id",
            "channel_id",
            "activity_date",
        ),
    )
    op.create_index(
        "ix_daily_text_activity_guild_date_user",
        "daily_text_activity",
        ["guild_id", "activity_date", "user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_daily_text_activity_guild_date_user",
        table_name="daily_text_activity",
    )
    op.drop_table("daily_text_activity")
