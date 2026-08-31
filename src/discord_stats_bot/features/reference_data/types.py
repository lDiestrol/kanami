"""Discord-independent snapshots used to provision persistence references."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class GuildSnapshot:
    id: int
    name: str | None


@dataclass(frozen=True, slots=True)
class DiscordUserSnapshot:
    id: int
    is_bot: bool
    username: str | None = None
    global_name: str | None = None
    avatar_hash: str | None = None


@dataclass(frozen=True, slots=True)
class GuildMemberSnapshot:
    guild_id: int
    user_id: int
    joined_at: datetime | None
    nickname: str | None = None
    has_complete_guild_identity: bool = True
    guild_avatar_hash: str | None = None


@dataclass(frozen=True, slots=True)
class VoiceChannelSnapshot:
    id: int
    guild_id: int
    name: str | None
    channel_kind: str
    is_afk: bool

    def __post_init__(self) -> None:
        if self.channel_kind not in {"voice", "stage"}:
            raise ValueError("channel_kind must be 'voice' or 'stage'")


@dataclass(frozen=True, slots=True)
class GuildReferenceSnapshot:
    guild: GuildSnapshot
    users: tuple[DiscordUserSnapshot, ...]
    members: tuple[GuildMemberSnapshot, ...]
    voice_channels: tuple[VoiceChannelSnapshot, ...]
