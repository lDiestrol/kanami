"""Discord-independent types for upcoming member anniversaries."""

from dataclasses import dataclass
from datetime import date, datetime

MEMBER_ANNIVERSARY_EVENT_TYPE = "member.anniversary"


@dataclass(frozen=True, slots=True)
class MemberJoinSnapshot:
    """Calendar-relevant member data obtained from the Discord guild cache."""

    user_id: int
    display_name: str
    joined_at: datetime | None
    is_bot: bool = False


@dataclass(frozen=True, slots=True)
class MemberAnniversary:
    """One member anniversary inside the requested future window."""

    user_id: int
    display_name: str
    anniversary_date: date
    years: int
    days_until: int
