from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class AuditEvent(Base):
    """Durable normalized event awaiting or recording Discord delivery."""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("guild_id > 0", name="ck_audit_events_guild_id_positive"),
        CheckConstraint(
            "subject_id IS NULL OR subject_id > 0",
            name="ck_audit_events_subject_id_positive",
        ),
        CheckConstraint(
            "actor_user_id IS NULL OR actor_user_id > 0",
            name="ck_audit_events_actor_user_id_positive",
        ),
        CheckConstraint(
            "channel_id IS NULL OR channel_id > 0",
            name="ck_audit_events_channel_id_positive",
        ),
        CheckConstraint(
            "discord_message_id IS NULL OR discord_message_id > 0",
            name="ck_audit_events_discord_message_id_positive",
        ),
        CheckConstraint(
            "delivery_attempts >= 0",
            name="ck_audit_events_delivery_attempts_nonnegative",
        ),
        Index(
            "ix_audit_events_guild_occurred_at",
            "guild_id",
            "occurred_at",
        ),
        Index(
            "ix_audit_events_guild_event_type_occurred_at",
            "guild_id",
            "event_type",
            "occurred_at",
        ),
        Index(
            "ix_audit_events_pending_delivery",
            "guild_id",
            "next_delivery_attempt_at",
            "occurred_at",
            "id",
            postgresql_where=text("delivered_at IS NULL"),
        ),
        Index(
            "ix_audit_events_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
        Index(
            "uq_audit_events_member_anniversary",
            "guild_id",
            "subject_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("event_type = 'member.anniversary'"),
        ),
        Index(
            "uq_audit_events_member_returned",
            "guild_id",
            "subject_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("event_type = 'member.returned'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    before_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    after_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    details_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    discord_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_delivery_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
