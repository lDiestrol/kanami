"""Persistence model for managed Web Admin access grants."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class WebAdminAccessGrant(Base):
    """One historical grant of managed Web Admin access for a guild user."""

    __tablename__ = "web_admin_access_grants"
    __table_args__ = (
        CheckConstraint(
            "guild_id > 0",
            name="ck_web_admin_access_grants_guild_id_positive",
        ),
        CheckConstraint(
            "user_id > 0",
            name="ck_web_admin_access_grants_user_id_positive",
        ),
        CheckConstraint(
            "granted_by_user_id > 0",
            name="ck_web_admin_access_grants_granted_by_positive",
        ),
        CheckConstraint(
            "revoked_by_user_id IS NULL OR revoked_by_user_id > 0",
            name="ck_web_admin_access_grants_revoked_by_positive",
        ),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by_user_id IS NULL)",
            name="ck_web_admin_access_grants_revocation_pair",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_web_admin_access_grants_revoked_after_granted",
        ),
        Index(
            "uq_web_admin_access_grants_active",
            "guild_id",
            "user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_web_admin_access_grants_guild_granted_at",
            "guild_id",
            "granted_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    granted_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    revoked_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
