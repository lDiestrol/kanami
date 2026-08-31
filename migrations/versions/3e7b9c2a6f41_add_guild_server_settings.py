"""add guild server settings

Revision ID: 3e7b9c2a6f41
Revises: 8d44cacc791e
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3e7b9c2a6f41"
down_revision: str | None = "8d44cacc791e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING_PREFIXES = (
    "autorole_role",
    "audit_log_channel",
    "anniversary_channel",
    "return_channel",
)


def upgrade() -> None:
    columns: list[sa.Column[object]] = [
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
    ]
    for prefix in SETTING_PREFIXES:
        columns.extend(
            (
                sa.Column(
                    f"{prefix}_mode",
                    sa.Text(),
                    server_default=sa.text("'env'"),
                    nullable=False,
                ),
                sa.Column(f"{prefix}_id", sa.BigInteger(), nullable=True),
            )
        )
    columns.extend(
        (
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column("updated_by_user_id", sa.BigInteger(), nullable=True),
        )
    )
    constraints: list[object] = [
        sa.CheckConstraint(
            "updated_by_user_id IS NULL OR updated_by_user_id > 0",
            name="ck_guild_server_settings_updated_by_positive",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id"],
            ["guilds.id"],
            name="fk_guild_server_settings_guild_id_guilds",
        ),
        sa.PrimaryKeyConstraint("guild_id"),
    ]
    for prefix in SETTING_PREFIXES:
        constraints.extend(
            (
                sa.CheckConstraint(
                    f"{prefix}_mode IN ('env', 'value', 'disabled')",
                    name=f"ck_guild_server_settings_{prefix}_mode",
                ),
                sa.CheckConstraint(
                    f"({prefix}_mode = 'value') = ({prefix}_id IS NOT NULL) "
                    f"AND ({prefix}_id IS NULL OR {prefix}_id > 0)",
                    name=f"ck_guild_server_settings_{prefix}_value",
                ),
            )
        )
    op.create_table("guild_server_settings", *columns, *constraints)


def downgrade() -> None:
    op.drop_table("guild_server_settings")
