from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class GuildMember(Base):
    """A Discord user's membership state in a guild."""

    __tablename__ = "guild_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id"],
            ["guilds.id"],
            name="fk_guild_members_guild_id_guilds",
        ),
        ForeignKeyConstraint(
            ["user_id"],
            ["discord_users.id"],
            name="fk_guild_members_user_id_discord_users",
        ),
        CheckConstraint(
            "left_at IS NULL OR joined_at IS NULL OR left_at >= joined_at",
            name="ck_guild_members_membership_time_order",
        ),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    left_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    nickname: Mapped[str | None] = mapped_column(Text, nullable=True)
    guild_avatar_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
