from sqlalchemy import BigInteger, CheckConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class Guild(Base):
    """Discord guild known to the application."""

    __tablename__ = "guilds"
    __table_args__ = (CheckConstraint("id > 0", name="ck_guilds_id_positive"),)

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
    )
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
