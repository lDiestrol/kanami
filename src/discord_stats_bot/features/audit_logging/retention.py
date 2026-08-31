"""Retention policy for durable audit events."""

from datetime import datetime, timedelta

from discord_stats_bot.features.audit_logging.types import AuditRetentionPolicy


def calculate_expires_at(
    occurred_at: datetime,
    policy: AuditRetentionPolicy,
    *,
    transient_retention_days: int,
) -> datetime | None:
    """Return an expiry timestamp for transient events only."""

    if transient_retention_days <= 0:
        raise ValueError("transient_retention_days must be positive")
    if policy is AuditRetentionPolicy.IMPORTANT:
        return None
    return occurred_at + timedelta(days=transient_retention_days)
