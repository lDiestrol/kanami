"""Add generic guild capability policies and grants.

Revision ID: 5c8e2a7d9f31
Revises: d4e8a1c7b962
Create Date: 2026-09-07
"""

import sqlalchemy as sa
from alembic import op

revision: str = "5c8e2a7d9f31"
down_revision: str | None = "d4e8a1c7b962"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "guild_capability_policies",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("capability_key", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_user_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "guild_id > 0", name="ck_capability_policies_guild_positive"
        ),
        sa.CheckConstraint(
            "length(btrim(capability_key)) > 0",
            name="ck_capability_policies_key_not_empty",
        ),
        sa.CheckConstraint(
            "updated_by_user_id > 0",
            name="ck_capability_policies_updated_by_positive",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id"],
            ["guilds.id"],
            name="fk_capability_policies_guild_id_guilds",
        ),
        sa.PrimaryKeyConstraint(
            "guild_id", "capability_key", name=op.f("pk_guild_capability_policies")
        ),
    )
    op.create_table(
        "guild_capability_grants",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("capability_key", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("guild_id > 0", name="ck_capability_grants_guild_positive"),
        sa.CheckConstraint(
            "length(btrim(capability_key)) > 0",
            name="ck_capability_grants_key_not_empty",
        ),
        sa.CheckConstraint(
            "subject_type IN ('user', 'role')",
            name="ck_capability_grants_subject_type",
        ),
        sa.CheckConstraint(
            "subject_id > 0", name="ck_capability_grants_subject_id_positive"
        ),
        sa.CheckConstraint(
            "created_by_user_id > 0",
            name="ck_capability_grants_created_by_positive",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id"],
            ["guilds.id"],
            name="fk_capability_grants_guild_id_guilds",
        ),
        sa.PrimaryKeyConstraint(
            "guild_id",
            "capability_key",
            "subject_type",
            "subject_id",
            name=op.f("pk_guild_capability_grants"),
        ),
    )


def downgrade() -> None:
    op.drop_table("guild_capability_grants")
    op.drop_table("guild_capability_policies")
