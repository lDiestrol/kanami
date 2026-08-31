"""add durable audit events

Revision ID: 91c4f28a6d3e
Revises: 6f3d2a91b7c4
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "91c4f28a6d3e"
down_revision: str | None = "6f3d2a91b7c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "before_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "after_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "details_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("discord_message_id", sa.BigInteger(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "delivery_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "next_delivery_attempt_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_delivery_error", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "actor_user_id IS NULL OR actor_user_id > 0",
            name="ck_audit_events_actor_user_id_positive",
        ),
        sa.CheckConstraint(
            "channel_id IS NULL OR channel_id > 0",
            name="ck_audit_events_channel_id_positive",
        ),
        sa.CheckConstraint(
            "delivery_attempts >= 0",
            name="ck_audit_events_delivery_attempts_nonnegative",
        ),
        sa.CheckConstraint(
            "discord_message_id IS NULL OR discord_message_id > 0",
            name="ck_audit_events_discord_message_id_positive",
        ),
        sa.CheckConstraint(
            "guild_id > 0",
            name="ck_audit_events_guild_id_positive",
        ),
        sa.CheckConstraint(
            "subject_id IS NULL OR subject_id > 0",
            name="ck_audit_events_subject_id_positive",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_expires_at",
        "audit_events",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_index(
        "ix_audit_events_guild_event_type_occurred_at",
        "audit_events",
        ["guild_id", "event_type", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_guild_occurred_at",
        "audit_events",
        ["guild_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_pending_delivery",
        "audit_events",
        ["guild_id", "next_delivery_attempt_at", "occurred_at", "id"],
        unique=False,
        postgresql_where=sa.text("delivered_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_pending_delivery", table_name="audit_events")
    op.drop_index("ix_audit_events_guild_occurred_at", table_name="audit_events")
    op.drop_index(
        "ix_audit_events_guild_event_type_occurred_at", table_name="audit_events"
    )
    op.drop_index("ix_audit_events_expires_at", table_name="audit_events")
    op.drop_table("audit_events")
