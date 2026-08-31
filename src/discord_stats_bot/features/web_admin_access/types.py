"""Discord-independent types for managed Web Admin access."""

from dataclasses import dataclass
from datetime import UTC, datetime


def normalize_utc_datetime(value: datetime, field_name: str) -> datetime:
    """Require a timezone-aware datetime and normalize it to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class WebAdminAccessGrantRecord:
    """One persisted historical Web Admin access grant."""

    id: int
    guild_id: int
    user_id: int
    granted_by_user_id: int
    granted_at: datetime
    revoked_by_user_id: int | None
    revoked_at: datetime | None

    def __post_init__(self) -> None:
        for field_name in ("id", "guild_id", "user_id", "granted_by_user_id"):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.revoked_by_user_id is not None and self.revoked_by_user_id <= 0:
            raise ValueError("revoked_by_user_id must be positive when provided")
        object.__setattr__(
            self, "granted_at", normalize_utc_datetime(self.granted_at, "granted_at")
        )
        if self.revoked_at is not None:
            object.__setattr__(
                self,
                "revoked_at",
                normalize_utc_datetime(self.revoked_at, "revoked_at"),
            )
        if (self.revoked_at is None) != (self.revoked_by_user_id is None):
            raise ValueError("revoked_at and revoked_by_user_id must be set together")
        if self.revoked_at is not None and self.revoked_at < self.granted_at:
            raise ValueError("revoked_at must not be before granted_at")
