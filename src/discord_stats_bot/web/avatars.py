"""Pure presentation helpers for trusted persisted Discord avatar assets."""

import re

MAX_POSTGRESQL_BIGINT = 9_223_372_036_854_775_807
_AVATAR_HASH = re.compile(r"(?:a_)?[0-9a-f]{32}\Z")
_ALLOWED_SIZES = frozenset({16, 32, 64, 128, 256, 512, 1024, 2048, 4096})
_CDN_ORIGIN = "https://cdn.discordapp.com"


def discord_member_avatar_url(
    *,
    guild_id: int,
    user_id: int,
    guild_avatar_hash: str | None,
    avatar_hash: str | None,
    size: int,
) -> str | None:
    """Return an allowlisted preferred Discord CDN avatar URL, if usable."""

    if not _valid_snowflake(guild_id) or not _valid_snowflake(user_id):
        return None
    if size not in _ALLOWED_SIZES:
        return None
    if _valid_avatar_hash(guild_avatar_hash):
        extension = "gif" if guild_avatar_hash.startswith("a_") else "png"
        return (
            f"{_CDN_ORIGIN}/guilds/{guild_id}/users/{user_id}/avatars/"
            f"{guild_avatar_hash}.{extension}?size={size}"
        )
    if _valid_avatar_hash(avatar_hash):
        extension = "gif" if avatar_hash.startswith("a_") else "png"
        return f"{_CDN_ORIGIN}/avatars/{user_id}/{avatar_hash}.{extension}?size={size}"
    return None


def _valid_snowflake(value: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (0 < value <= MAX_POSTGRESQL_BIGINT)
    )


def _valid_avatar_hash(value: str | None) -> bool:
    return isinstance(value, str) and _AVATAR_HASH.fullmatch(value) is not None
