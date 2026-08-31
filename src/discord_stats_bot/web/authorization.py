"""Discord-independent access policy for Kanami Web Admin."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class CurrentGuildMembershipRepository(Protocol):
    """Read only the membership fact needed by the access policy."""

    async def is_current_non_bot_member(self, discord_user_id: int) -> bool: ...


class ManagedWebAdminAccessRepository(Protocol):
    """Read only the active managed-admin fact needed by the access policy."""

    async def is_active_admin(self, discord_user_id: int) -> bool: ...


class WebAdminRole(StrEnum):
    """The complete Web Admin role set for managed authorization."""

    OWNER = "owner"
    ADMIN = "admin"


class WebAdminAuthorizationCategory(StrEnum):
    NOT_ALLOWED = "not_allowed"
    NOT_CURRENT_MEMBER = "not_current_member"


@dataclass(frozen=True, slots=True)
class WebAdminAuthorizationDecision:
    allowed: bool
    category: WebAdminAuthorizationCategory | None = None
    role: WebAdminRole | None = None

    def __post_init__(self) -> None:
        if self.allowed != (self.role is not None):
            raise ValueError("allowed authorization must have exactly one role")
        if self.allowed == (self.category is not None):
            raise ValueError("denied authorization must have exactly one category")


class WebAdminAuthorizationService:
    """Resolve OWNER/ADMIN access, then require current guild membership."""

    def __init__(
        self,
        owner_user_ids: frozenset[int],
        managed_access_repository: ManagedWebAdminAccessRepository,
        membership_repository: CurrentGuildMembershipRepository,
    ) -> None:
        self._owner_user_ids = owner_user_ids
        self._managed_access_repository = managed_access_repository
        self._membership_repository = membership_repository

    async def authorize(self, discord_user_id: int) -> WebAdminAuthorizationDecision:
        if discord_user_id in self._owner_user_ids:
            role = WebAdminRole.OWNER
        elif await self._managed_access_repository.is_active_admin(discord_user_id):
            role = WebAdminRole.ADMIN
        else:
            return WebAdminAuthorizationDecision(
                False,
                WebAdminAuthorizationCategory.NOT_ALLOWED,
            )
        if not await self._membership_repository.is_current_non_bot_member(
            discord_user_id
        ):
            return WebAdminAuthorizationDecision(
                False,
                WebAdminAuthorizationCategory.NOT_CURRENT_MEMBER,
            )
        return WebAdminAuthorizationDecision(True, role=role)
