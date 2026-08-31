"""Persistence model for guild-specific server setting overrides."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class GuildServerSettings(Base):
    """One independent tri-state override set for a configured guild."""

    __tablename__ = "guild_server_settings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "rules_publication_ruleset_id"],
            ["rulesets.guild_id", "rulesets.id"],
            name="fk_guild_server_settings_rules_publication_ruleset",
        ),
        CheckConstraint(
            "updated_by_user_id IS NULL OR updated_by_user_id > 0",
            name="ck_guild_server_settings_updated_by_positive",
        ),
        *(
            CheckConstraint(
                f"{prefix}_mode IN ('env', 'value', 'disabled')",
                name=f"ck_guild_server_settings_{prefix}_mode",
            )
            for prefix in (
                "autorole_role",
                "audit_log_channel",
                "anniversary_channel",
                "return_channel",
            )
        ),
        CheckConstraint(
            "rules_publication_channel_id IS NULL OR rules_publication_channel_id > 0",
            name="ck_guild_server_settings_rules_publication_channel_positive",
        ),
        CheckConstraint(
            "rules_publication_message_id IS NULL OR rules_publication_message_id > 0",
            name="ck_guild_server_settings_rules_publication_message_positive",
        ),
        CheckConstraint(
            "(rules_publication_message_id IS NULL) = "
            "(rules_publication_ruleset_id IS NULL)",
            name="ck_guild_server_settings_rules_publication_delivery_state",
        ),
        *(
            CheckConstraint(
                f"({prefix}_mode = 'value') = ({prefix}_id IS NOT NULL) "
                f"AND ({prefix}_id IS NULL OR {prefix}_id > 0)",
                name=f"ck_guild_server_settings_{prefix}_value",
            )
            for prefix in (
                "autorole_role",
                "audit_log_channel",
                "anniversary_channel",
                "return_channel",
            )
        ),
    )

    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guilds.id", name="fk_guild_server_settings_guild_id_guilds"),
        primary_key=True,
        autoincrement=False,
    )
    autorole_role_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="env", server_default=text("'env'")
    )
    autorole_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    audit_log_channel_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="env", server_default=text("'env'")
    )
    audit_log_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    anniversary_channel_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="env", server_default=text("'env'")
    )
    anniversary_channel_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    return_channel_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="env", server_default=text("'env'")
    )
    return_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rules_publication_channel_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    rules_publication_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    rules_publication_ruleset_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
