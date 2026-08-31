"""Add durable managed Rules publication state.

Revision ID: e1a7c4d92b60
Revises: b6e2c8f91a47
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a7c4d92b60"
down_revision: str | None = "b6e2c8f91a47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "guild_server_settings",
        sa.Column("rules_publication_channel_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "guild_server_settings",
        sa.Column("rules_publication_message_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "guild_server_settings",
        sa.Column("rules_publication_ruleset_id", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_guild_server_settings_rules_publication_channel_positive",
        "guild_server_settings",
        "rules_publication_channel_id IS NULL OR rules_publication_channel_id > 0",
    )
    op.create_check_constraint(
        "ck_guild_server_settings_rules_publication_message_positive",
        "guild_server_settings",
        "rules_publication_message_id IS NULL OR rules_publication_message_id > 0",
    )
    op.create_check_constraint(
        "ck_guild_server_settings_rules_publication_delivery_state",
        "guild_server_settings",
        "(rules_publication_message_id IS NULL) = "
        "(rules_publication_ruleset_id IS NULL)",
    )
    op.create_foreign_key(
        "fk_guild_server_settings_rules_publication_ruleset",
        "guild_server_settings",
        "rulesets",
        ["guild_id", "rules_publication_ruleset_id"],
        ["guild_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_guild_server_settings_rules_publication_ruleset",
        "guild_server_settings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_guild_server_settings_rules_publication_delivery_state",
        "guild_server_settings",
        type_="check",
    )
    op.drop_constraint(
        "ck_guild_server_settings_rules_publication_message_positive",
        "guild_server_settings",
        type_="check",
    )
    op.drop_constraint(
        "ck_guild_server_settings_rules_publication_channel_positive",
        "guild_server_settings",
        type_="check",
    )
    op.drop_column("guild_server_settings", "rules_publication_ruleset_id")
    op.drop_column("guild_server_settings", "rules_publication_message_id")
    op.drop_column("guild_server_settings", "rules_publication_channel_id")
