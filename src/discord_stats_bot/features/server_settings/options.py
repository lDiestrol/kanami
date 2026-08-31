"""Discord-independent bounded server-setting option values."""

from dataclasses import dataclass
from enum import StrEnum


class ServerSettingsChannelType(StrEnum):
    TEXT = "text"
    NEWS = "news"


@dataclass(frozen=True, slots=True)
class ServerSettingsRoleOption:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class ServerSettingsChannelOption:
    id: int
    name: str
    type: ServerSettingsChannelType


@dataclass(frozen=True, slots=True)
class ServerSettingsOptions:
    roles: tuple[ServerSettingsRoleOption, ...]
    channels: tuple[ServerSettingsChannelOption, ...]
