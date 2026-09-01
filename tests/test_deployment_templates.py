from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NGINX_TEMPLATES = (
    REPOSITORY_ROOT / "deploy/nginx/kanami-remote-proxy.conf.example",
    REPOSITORY_ROOT / "deploy/nginx/kanami-same-host.conf.example",
)
CADDY_TEMPLATE = REPOSITORY_ROOT / "deploy/caddy/Caddyfile.example"
WEB_ADMIN_UNIT = REPOSITORY_ROOT / "deploy/systemd/kanami-web-admin.service"


@pytest.mark.parametrize("template_path", NGINX_TEMPLATES)
def test_nginx_template_enforces_public_web_admin_routes(
    template_path: Path,
) -> None:
    config = template_path.read_text(encoding="utf-8")

    root_redirect = "location = / {\n        return 302 /admin/;\n    }"
    web_admin_route = "location /admin/ {"
    closed_catch_all = "location / {\n        return 404;\n    }"

    assert root_redirect in config
    assert web_admin_route in config
    assert closed_catch_all in config
    assert config.index(root_redirect) < config.index(web_admin_route)
    assert config.index(web_admin_route) < config.index(closed_catch_all)
    assert "proxy_pass http://127.0.0.1:8765" not in config
    assert "location /control" not in config
    assert "location = /control" not in config


def test_caddy_template_enforces_public_web_admin_routes() -> None:
    config = CADDY_TEMPLATE.read_text(encoding="utf-8")

    root_redirect = "@root path /\n        redir @root /admin/ 302"
    web_admin_route = "@web_admin path /admin/*"
    scoped_proxy = "reverse_proxy @web_admin 127.0.0.1:8000"
    closed_catch_all = "respond 404"

    assert "route {" in config
    assert root_redirect in config
    assert web_admin_route in config
    assert scoped_proxy in config
    assert closed_catch_all in config
    assert config.index(root_redirect) < config.index(web_admin_route)
    assert config.index(web_admin_route) < config.index(scoped_proxy)
    assert config.index(scoped_proxy) < config.index(closed_catch_all)
    assert "127.0.0.1:8765" not in config
    assert "/control" not in config


def test_remote_nginx_template_uses_non_production_example_address() -> None:
    config = NGINX_TEMPLATES[0].read_text(encoding="utf-8")

    assert "server 192.168.50.10:8000;" in config


def test_canonical_web_admin_unit_uses_isolated_runtime_and_hardening() -> None:
    unit = WEB_ADMIN_UNIT.read_text(encoding="utf-8")

    for required in (
        "User=kanami-web",
        "Group=kanami-web",
        "WorkingDirectory=/opt/kanami",
        "EnvironmentFile=/etc/kanami/kanami-web-admin.env",
        "Environment=HOME=/var/lib/kanami-web",
        "ExecStart=/var/lib/kanami-web/.venv/bin/kanami-web-admin",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
        "RestrictSUIDSGID=true",
        "LockPersonality=true",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    ):
        assert required in unit
    assert "/opt/kanami/.venv/bin/kanami-web-admin" not in unit
