"""Read-only Kanami Web Admin process."""

from discord_stats_bot.config import WebSettings
from discord_stats_bot.web.app import create_app

__all__ = ["WebSettings", "create_app"]
