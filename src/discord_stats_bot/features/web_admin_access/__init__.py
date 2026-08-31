"""Managed Web Admin access domain."""

from discord_stats_bot.features.web_admin_access.service import (
    WEB_ADMIN_ACCESS_GRANTED_EVENT_TYPE,
    WEB_ADMIN_ACCESS_REVOKED_EVENT_TYPE,
    WEB_ADMIN_AUDIT_CATEGORY,
    WebAdminAccessService,
)
from discord_stats_bot.features.web_admin_access.types import (
    WebAdminAccessGrantRecord,
    normalize_utc_datetime,
)

__all__ = [
    "WEB_ADMIN_ACCESS_GRANTED_EVENT_TYPE",
    "WEB_ADMIN_ACCESS_REVOKED_EVENT_TYPE",
    "WEB_ADMIN_AUDIT_CATEGORY",
    "WebAdminAccessGrantRecord",
    "WebAdminAccessService",
    "normalize_utc_datetime",
]
