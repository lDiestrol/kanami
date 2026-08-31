"""Add versioned rulesets and durable rule acceptances.

Revision ID: b6e2c8f91a47
Revises: f2a6c9d41b73
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6e2c8f91a47"
down_revision: str | None = "f2a6c9d41b73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rulesets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("requires_reacceptance", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id > 0", name="ck_rulesets_id_positive"),
        sa.CheckConstraint(
            "btrim(version) <> ''", name="ck_rulesets_version_not_blank"
        ),
        sa.CheckConstraint("btrim(title) <> ''", name="ck_rulesets_title_not_blank"),
        sa.CheckConstraint(
            "btrim(content) <> ''", name="ck_rulesets_content_not_blank"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name="ck_rulesets_status",
        ),
        sa.CheckConstraint(
            "(status = 'draft' AND published_at IS NULL) OR "
            "(status IN ('published', 'archived') AND published_at IS NOT NULL)",
            name="ck_rulesets_publication_state",
        ),
        sa.CheckConstraint(
            "created_by IS NULL OR created_by > 0",
            name="ck_rulesets_created_by_positive",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id"], ["guilds.id"], name="fk_rulesets_guild_id_guilds"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["discord_users.id"],
            name="fk_rulesets_created_by_discord_users",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guild_id", "version", name="uq_rulesets_guild_version"),
        sa.UniqueConstraint("guild_id", "id", name="uq_rulesets_guild_id_id"),
    )
    op.create_index(
        "uq_rulesets_current_published_guild",
        "rulesets",
        ["guild_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_table(
        "rule_acceptances",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("ruleset_id", sa.BigInteger(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id > 0", name="ck_rule_acceptances_id_positive"),
        sa.ForeignKeyConstraint(
            ["guild_id", "user_id"],
            ["guild_members.guild_id", "guild_members.user_id"],
            name="fk_rule_acceptances_guild_user_guild_members",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "ruleset_id"],
            ["rulesets.guild_id", "rulesets.id"],
            name="fk_rule_acceptances_guild_ruleset_rulesets",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id",
            "user_id",
            "ruleset_id",
            name="uq_rule_acceptances_guild_user_ruleset",
        ),
    )
    op.create_index(
        "ix_rule_acceptances_guild_ruleset_accepted_at",
        "rule_acceptances",
        ["guild_id", "ruleset_id", "accepted_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rule_acceptances_guild_ruleset_accepted_at",
        table_name="rule_acceptances",
    )
    op.drop_table("rule_acceptances")
    op.drop_index("uq_rulesets_current_published_guild", table_name="rulesets")
    op.drop_table("rulesets")
