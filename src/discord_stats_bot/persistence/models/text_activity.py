"""Daily aggregate model for Discord text activity."""

from datetime import date

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class DailyTextActivity(Base):
    """One aggregate per guild member, channel, and reporting date."""

    __tablename__ = "daily_text_activity"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "user_id"],
            ["guild_members.guild_id", "guild_members.user_id"],
            name="fk_daily_text_activity_guild_member",
        ),
        CheckConstraint(
            "channel_id > 0",
            name="ck_daily_text_activity_channel_id_positive",
        ),
        CheckConstraint(
            "message_count >= 0",
            name="ck_daily_text_activity_message_count_nonnegative",
        ),
        CheckConstraint(
            "attachment_count >= 0",
            name="ck_daily_text_activity_attachment_count_nonnegative",
        ),
        CheckConstraint(
            "reply_count >= 0",
            name="ck_daily_text_activity_reply_count_nonnegative",
        ),
        Index(
            "ix_daily_text_activity_guild_date_user",
            "guild_id",
            "activity_date",
            "user_id",
        ),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    activity_date: Mapped[date] = mapped_column(Date, primary_key=True)
    message_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attachment_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reply_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
