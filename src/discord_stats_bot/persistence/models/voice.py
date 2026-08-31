from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class VoiceChannel(Base):
    """Discord voice or stage channel."""

    __tablename__ = "voice_channels"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id"],
            ["guilds.id"],
            name="fk_voice_channels_guild_id_guilds",
        ),
        UniqueConstraint(
            "guild_id",
            "id",
            name="uq_voice_channels_guild_id_id",
        ),
        CheckConstraint("id > 0", name="ck_voice_channels_id_positive"),
        CheckConstraint(
            "channel_kind IN ('voice', 'stage')",
            name="ck_voice_channels_channel_kind",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
    )
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_kind: Mapped[str] = mapped_column(Text, nullable=False)
    is_afk: Mapped[bool] = mapped_column(Boolean, nullable=False)


class VoiceSession(Base):
    """One logical continuous connection of a guild member to voice."""

    __tablename__ = "voice_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "user_id"],
            ["guild_members.guild_id", "guild_members.user_id"],
            name="fk_voice_sessions_guild_member",
        ),
        UniqueConstraint(
            "id",
            "guild_id",
            "user_id",
            name="uq_voice_sessions_id_guild_id_user_id",
        ),
        CheckConstraint(
            "started_at <= confirmed_through_at",
            name="ck_voice_sessions_started_before_confirmed",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_voice_sessions_end_after_start",
        ),
        CheckConstraint(
            "ended_at IS NULL OR confirmed_through_at <= ended_at",
            name="ck_voice_sessions_confirmed_before_end",
        ),
        Index(
            "uq_voice_sessions_open_guild_user",
            "guild_id",
            "user_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_voice_sessions_guild_user_started_at",
            "guild_id",
            "user_id",
            "started_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    confirmed_through_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class VoiceInterval(Base):
    """Atomic channel interval within a logical voice session."""

    __tablename__ = "voice_intervals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "guild_id", "user_id"],
            [
                "voice_sessions.id",
                "voice_sessions.guild_id",
                "voice_sessions.user_id",
            ],
            name="fk_voice_intervals_session_guild_user",
        ),
        ForeignKeyConstraint(
            ["guild_id", "channel_id"],
            ["voice_channels.guild_id", "voice_channels.id"],
            name="fk_voice_intervals_guild_channel",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_voice_intervals_end_after_start",
        ),
        CheckConstraint(
            "quality IN ('exact', 'estimated')",
            name="ck_voice_intervals_quality",
        ),
        CheckConstraint(
            "channel_kind IN ('voice', 'stage')",
            name="ck_voice_intervals_channel_kind",
        ),
        CheckConstraint(
            "ended_at IS NOT NULL OR quality = 'exact'",
            name="ck_voice_intervals_open_must_be_exact",
        ),
        Index(
            "uq_voice_intervals_open_guild_user",
            "guild_id",
            "user_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_voice_intervals_session_started_at",
            "session_id",
            "started_at",
        ),
        Index(
            "ix_voice_intervals_guild_user_started_at",
            "guild_id",
            "user_id",
            "started_at",
        ),
        Index(
            "ix_voice_intervals_guild_channel_started_at",
            "guild_id",
            "channel_id",
            "started_at",
        ),
        Index(
            "ix_voice_intervals_guild_started_at",
            "guild_id",
            "started_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    quality: Mapped[str] = mapped_column(Text, nullable=False)
    channel_kind: Mapped[str] = mapped_column(Text, nullable=False)
    is_afk: Mapped[bool] = mapped_column(Boolean, nullable=False)
