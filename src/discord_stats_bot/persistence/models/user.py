from sqlalchemy import BigInteger, Boolean, CheckConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class DiscordUser(Base):
    """Global Discord user identity."""

    __tablename__ = "discord_users"
    __table_args__ = (CheckConstraint("id > 0", name="ck_discord_users_id_positive"),)

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
    )
    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    global_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
