"""Console entry point for the standalone Kanami Web Admin process."""

import logging
from ipaddress import ip_address

import uvicorn

from discord_stats_bot.config import WebSettings
from discord_stats_bot.logging import configure_logging
from discord_stats_bot.web.app import create_app


def main() -> int:
    settings = WebSettings()
    configure_logging(settings.log_level)
    if not ip_address(settings.web_admin_host).is_loopback:
        logging.getLogger(__name__).warning(
            "Web Admin is listening on a private non-loopback interface; "
            "protect it with a firewall and trusted reverse proxy."
        )
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.web_admin_host,
        port=settings.web_admin_port,
        log_level=settings.log_level.lower(),
        access_log=False,
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
