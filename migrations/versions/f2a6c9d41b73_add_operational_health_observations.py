"""Add low-frequency operational health observations.

Revision ID: f2a6c9d41b73
Revises: c5b7e1d9a024
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a6c9d41b73"
down_revision: str | None = "c5b7e1d9a024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operational_health_observations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("overall_status", sa.Text(), nullable=False),
        sa.Column("discord_status", sa.Text(), nullable=False),
        sa.Column("postgresql_status", sa.Text(), nullable=False),
        sa.Column("voice_status", sa.Text(), nullable=False),
        sa.Column("game_status", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "overall_status IN ('healthy', 'degraded', 'unavailable')",
            name="ck_operational_health_overall_status",
        ),
        sa.CheckConstraint(
            "discord_status IN ('healthy', 'degraded', 'unavailable')",
            name="ck_operational_health_discord_status",
        ),
        sa.CheckConstraint(
            "postgresql_status IN ('healthy', 'degraded', 'unavailable')",
            name="ck_operational_health_postgresql_status",
        ),
        sa.CheckConstraint(
            "voice_status IN ('healthy', 'degraded', 'unavailable')",
            name="ck_operational_health_voice_status",
        ),
        sa.CheckConstraint(
            "game_status IN ('healthy', 'degraded', 'unavailable', 'neutral')",
            name="ck_operational_health_game_status",
        ),
        sa.CheckConstraint(
            "btrim(component) <> ''",
            name="ck_operational_health_component_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name="ck_operational_health_reason_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id"], ["guilds.id"], name="fk_operational_health_guild"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operational_health_guild_observed_at",
        "operational_health_observations",
        ["guild_id", "observed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operational_health_guild_observed_at",
        table_name="operational_health_observations",
    )
    op.drop_table("operational_health_observations")
