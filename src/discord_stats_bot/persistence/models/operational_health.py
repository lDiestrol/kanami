"""Low-frequency operational health observations owned by the bot runtime."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class OperationalHealthObservation(Base):
    """One bounded, operator-safe health classification at a point in time."""

    __tablename__ = "operational_health_observations"
    __table_args__ = (
        CheckConstraint(
            "overall_status IN ('healthy', 'degraded', 'unavailable')",
            name="ck_operational_health_overall_status",
        ),
        CheckConstraint(
            "discord_status IN ('healthy', 'degraded', 'unavailable')",
            name="ck_operational_health_discord_status",
        ),
        CheckConstraint(
            "postgresql_status IN ('healthy', 'degraded', 'unavailable')",
            name="ck_operational_health_postgresql_status",
        ),
        CheckConstraint(
            "voice_status IN ('healthy', 'degraded', 'unavailable')",
            name="ck_operational_health_voice_status",
        ),
        CheckConstraint(
            "game_status IN ('healthy', 'degraded', 'unavailable', 'neutral')",
            name="ck_operational_health_game_status",
        ),
        CheckConstraint(
            "btrim(component) <> ''",
            name="ck_operational_health_component_not_blank",
        ),
        CheckConstraint(
            "btrim(reason) <> ''",
            name="ck_operational_health_reason_not_blank",
        ),
        Index(
            "ix_operational_health_guild_observed_at",
            "guild_id",
            "observed_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guilds.id", name="fk_operational_health_guild"),
        nullable=False,
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    overall_status: Mapped[str] = mapped_column(Text, nullable=False)
    discord_status: Mapped[str] = mapped_column(Text, nullable=False)
    postgresql_status: Mapped[str] = mapped_column(Text, nullable=False)
    voice_status: Mapped[str] = mapped_column(Text, nullable=False)
    game_status: Mapped[str] = mapped_column(Text, nullable=False)
    component: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
