"""Discord-independent selection rules for optional automatic role assignment."""

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class AutoroleService:
    """Decide whether a guild member is eligible for the configured autorole."""

    guild_id: int
    role_id: int

    def __post_init__(self) -> None:
        if self.guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if self.role_id <= 0:
            raise ValueError("role_id must be positive")

    def should_consider(
        self,
        *,
        guild_id: int,
        is_bot: bool,
        member_role_ids: Iterable[int],
    ) -> bool:
        """Return whether Discord-specific role validation should continue."""

        return (
            guild_id == self.guild_id
            and not is_bot
            and self.role_id not in member_role_ids
        )
