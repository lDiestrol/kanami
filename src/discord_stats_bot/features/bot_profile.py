"""Shared validation and wire-safe types for the bot guild profile feature."""

from dataclasses import dataclass
from enum import StrEnum

BOT_NICKNAME_MAX_LENGTH = 32
BOT_AVATAR_MAX_BYTES = 8 * 1024 * 1024
PNG_CONTENT_TYPE = "image/png"
JPEG_CONTENT_TYPE = "image/jpeg"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class BotProfileErrorCategory(StrEnum):
    CONTROL_UNAVAILABLE = "control_unavailable"
    CONTROL_UNAUTHORIZED = "control_unauthorized"
    BOT_NOT_READY = "bot_not_ready"
    GUILD_UNAVAILABLE = "guild_unavailable"
    INVALID_NICKNAME = "invalid_nickname"
    INVALID_AVATAR = "invalid_avatar"
    DISCORD_FORBIDDEN = "discord_forbidden"
    DISCORD_API_FAILURE = "discord_api_failure"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"


class BotProfileOperationError(RuntimeError):
    """Content-free categorized failure safe for HTTP responses and logs."""

    def __init__(self, category: BotProfileErrorCategory) -> None:
        self.category = category
        super().__init__(category.value)


@dataclass(frozen=True, slots=True)
class BotGuildProfile:
    user_id: int
    application_name: str
    display_name: str
    nickname: str | None
    guild_avatar_url: str | None
    display_avatar_url: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "application_name": self.application_name,
            "display_name": self.display_name,
            "nickname": self.nickname,
            "guild_avatar_url": self.guild_avatar_url,
            "display_avatar_url": self.display_avatar_url,
        }


def normalize_bot_nickname(value: object) -> str:
    if not isinstance(value, str):
        raise BotProfileOperationError(BotProfileErrorCategory.INVALID_NICKNAME)
    nickname = value.strip()
    if (
        not nickname
        or len(nickname) > BOT_NICKNAME_MAX_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in nickname)
    ):
        raise BotProfileOperationError(BotProfileErrorCategory.INVALID_NICKNAME)
    return nickname


def validate_bot_avatar(data: bytes, content_type: str) -> str:
    if not data or len(data) > BOT_AVATAR_MAX_BYTES:
        raise BotProfileOperationError(BotProfileErrorCategory.INVALID_AVATAR)
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_content_type == PNG_CONTENT_TYPE:
        if not data.startswith(PNG_SIGNATURE):
            raise BotProfileOperationError(BotProfileErrorCategory.INVALID_AVATAR)
        return PNG_CONTENT_TYPE
    if normalized_content_type == JPEG_CONTENT_TYPE:
        if (
            len(data) < 5
            or not data.startswith(b"\xff\xd8\xff")
            or not data.endswith(b"\xff\xd9")
        ):
            raise BotProfileOperationError(BotProfileErrorCategory.INVALID_AVATAR)
        return JPEG_CONTENT_TYPE
    raise BotProfileOperationError(BotProfileErrorCategory.INVALID_AVATAR)
