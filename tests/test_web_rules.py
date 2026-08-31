from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError
from starlette.testclient import TestClient

import discord_stats_bot.web.rules as web_rules_module
from discord_stats_bot.config import WebSettings
from discord_stats_bot.features.rules import (
    DuplicateRulesetVersionError,
    ImmutableRulesetError,
    RuleAcceptanceRecord,
    RulesComplianceAvailability,
    RulesComplianceSummary,
    RulesetRecord,
    RulesetStatus,
    RulesetWithAcceptanceCount,
    RulesPublicationConfigurationResult,
    RulesPublicationConfigurationStatus,
    RulesPublicationState,
    RulesPublicationSyncResult,
    RulesPublicationSyncStatus,
    validate_ruleset_for_discord,
)
from discord_stats_bot.features.server_settings import (
    ServerSettingsChannelOption,
    ServerSettingsChannelType,
    ServerSettingsOptions,
)
from discord_stats_bot.web.app import create_app
from discord_stats_bot.web.auth import SESSION_COOKIE_NAME, SESSION_COOKIE_PATH
from discord_stats_bot.web.authorization import (
    WebAdminAuthorizationCategory,
    WebAdminAuthorizationDecision,
    WebAdminRole,
)
from discord_stats_bot.web.bot_control import (
    RulesPublicationControlCategory,
    RulesPublicationControlError,
)
from discord_stats_bot.web.rules import WebAdminRulesService
from discord_stats_bot.web.service import (
    AdminCounts,
    AdminMemberDetailResult,
    AdminMemberDetailStatus,
    AdminMembersPage,
    WebDatabaseHealth,
)

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def settings() -> WebSettings:
    return WebSettings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
        DISCORD_GUILD_ID=10,
        WEB_ADMIN_DISCORD_CLIENT_ID=123,
        WEB_ADMIN_DISCORD_CLIENT_SECRET="secret",
        WEB_ADMIN_DISCORD_REDIRECT_URI=(
            "http://localhost:8000/admin/auth/discord/callback"
        ),
        WEB_ADMIN_COOKIE_SECURE=False,
        WEB_ADMIN_ALLOWED_USER_IDS="41",
    )


def rule(
    ruleset_id: int,
    version: str,
    status: RulesetStatus,
    *,
    requires_reacceptance: bool = False,
) -> RulesetRecord:
    return RulesetRecord(
        ruleset_id,
        10,
        version,
        f"Правила {version}",
        f"Текст версии {version}",
        status,
        f"Изменения {version}",
        requires_reacceptance,
        41,
        NOW,
        NOW if status is not RulesetStatus.DRAFT else None,
    )


class FakeResources:
    session_factory = object()

    async def dispose(self) -> None:
        pass


class FakeAdminService:
    async def probe_database(self) -> WebDatabaseHealth:
        return WebDatabaseHealth(True, 0.001)

    async def load_counts(self) -> AdminCounts:
        return AdminCounts(1, 1, 1)

    async def load_members(
        self, *, page: int, query: str, **kwargs: object
    ) -> AdminMembersPage:
        return AdminMembersPage((), 0, page, 50, query)

    async def load_member_detail(self, user_id: int) -> AdminMemberDetailResult:
        return AdminMemberDetailResult(AdminMemberDetailStatus.NOT_FOUND)


class FakeAuthorizer:
    async def authorize(self, user_id: int) -> WebAdminAuthorizationDecision:
        role = {41: WebAdminRole.OWNER, 50: WebAdminRole.ADMIN}.get(user_id)
        if role is None:
            return WebAdminAuthorizationDecision(
                False, WebAdminAuthorizationCategory.NOT_ALLOWED
            )
        return WebAdminAuthorizationDecision(True, role=role)


class FakeRulesService:
    def __init__(self) -> None:
        self.rules = {
            1: rule(1, "1.0", RulesetStatus.PUBLISHED),
            2: rule(2, "0.9", RulesetStatus.ARCHIVED),
            3: rule(3, "1.1", RulesetStatus.DRAFT),
        }
        self.counts = {1: 7, 2: 3, 3: 0}
        self.mutations: list[tuple[str, object]] = []
        self.acceptance_name = "Участник"
        self.publication_state = RulesPublicationState(10, None, None, None)
        self.compliance_summary = RulesComplianceSummary(
            RulesComplianceAvailability.AVAILABLE,
            10,
            total=12,
            compliant=7,
            pending=3,
            overdue=2,
            current_ruleset_id=1,
            current_version="1.0",
            required_checkpoint_ruleset_id=1,
            required_checkpoint_version="1.0",
            checkpoint_requires_reacceptance=False,
        )
        self.compliance_error: Exception | None = None

    async def load(self) -> tuple[RulesetWithAcceptanceCount, ...]:
        return tuple(
            RulesetWithAcceptanceCount(item, self.counts[item.id])
            for item in self.rules.values()
        )

    async def load_publication_state(self) -> RulesPublicationState:
        return self.publication_state

    async def load_compliance_summary(self) -> RulesComplianceSummary:
        if self.compliance_error is not None:
            raise self.compliance_error
        return self.compliance_summary

    async def get(self, ruleset_id: int) -> RulesetRecord:
        return self.rules[ruleset_id]

    async def acceptances(self, ruleset_id: int) -> tuple[RuleAcceptanceRecord, ...]:
        return (RuleAcceptanceRecord(100, self.acceptance_name, NOW),)

    async def create_draft(self, version: str, actor_user_id: int) -> RulesetRecord:
        if any(item.version == version for item in self.rules.values()):
            raise DuplicateRulesetVersionError
        current = self.rules[1]
        validate_ruleset_for_discord(
            version=version.strip(), title=current.title, content=current.content
        )
        created = RulesetRecord(
            4,
            10,
            version,
            current.title,
            current.content,
            RulesetStatus.DRAFT,
            None,
            False,
            actor_user_id,
            NOW,
            None,
        )
        self.rules[4] = created
        self.counts[4] = 0
        self.mutations.append(("rules.draft_created", created))
        return created

    async def update_draft(
        self, ruleset_id: int, actor_user_id: int, **values: object
    ) -> RulesetRecord:
        current = self.rules[ruleset_id]
        if current.status is not RulesetStatus.DRAFT:
            raise ImmutableRulesetError
        validate_ruleset_for_discord(
            version=str(values["version"]).strip(),
            title=str(values["title"]).strip(),
            content=str(values["content"]).strip(),
        )
        updated = RulesetRecord(
            current.id,
            current.guild_id,
            str(values["version"]),
            str(values["title"]),
            str(values["content"]),
            current.status,
            str(values["change_summary"]),
            bool(values["requires_reacceptance"]),
            current.created_by,
            current.created_at,
            None,
            values.get("reacceptance_grace_days"),
        )
        self.rules[ruleset_id] = updated
        self.mutations.append(("rules.draft_updated", actor_user_id))
        return updated

    async def delete_draft(self, ruleset_id: int, actor_user_id: int) -> None:
        if self.rules[ruleset_id].status is not RulesetStatus.DRAFT:
            raise ImmutableRulesetError
        del self.rules[ruleset_id]
        self.mutations.append(("rules.draft_deleted", actor_user_id))

    async def publish(self, ruleset_id: int, actor_user_id: int) -> RulesetRecord:
        draft = self.rules[ruleset_id]
        if draft.status is not RulesetStatus.DRAFT:
            raise ImmutableRulesetError
        validate_ruleset_for_discord(
            version=draft.version, title=draft.title, content=draft.content
        )
        old = self.rules[1]
        self.rules[1] = replace(old, status=RulesetStatus.ARCHIVED)
        published = replace(draft, status=RulesetStatus.PUBLISHED, published_at=NOW)
        self.rules[ruleset_id] = published
        self.mutations.append(("rules.published", actor_user_id))
        return published


class FakeRulesPublicationControl:
    def __init__(self) -> None:
        self.options = ServerSettingsOptions(
            (),
            (
                ServerSettingsChannelOption(
                    20, "правила", ServerSettingsChannelType.TEXT
                ),
                ServerSettingsChannelOption(
                    21, "объявления", ServerSettingsChannelType.NEWS
                ),
            ),
        )
        self.options_error: Exception | None = None
        self.sync_result = RulesPublicationSyncResult(
            RulesPublicationSyncStatus.NOT_CONFIGURED, 10
        )
        self.configuration_result = RulesPublicationConfigurationResult(
            RulesPublicationConfigurationStatus.CONFIGURED,
            10,
            None,
            20,
            None,
        )
        self.error: Exception | None = None
        self.calls: list[tuple[object, ...]] = []

    async def get_server_settings_options(self) -> ServerSettingsOptions:
        self.calls.append(("options",))
        if self.options_error is not None:
            raise self.options_error
        return self.options

    async def sync_rules_publication(
        self, *, actor_discord_user_id: int
    ) -> RulesPublicationSyncResult:
        self.calls.append(("sync", actor_discord_user_id))
        if self.error is not None:
            raise self.error
        return self.sync_result

    async def configure_rules_publication(
        self, channel_id: int, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult:
        self.calls.append(("configure", channel_id, actor_discord_user_id))
        if self.error is not None:
            raise self.error
        return self.configuration_result

    async def disable_rules_publication(
        self, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult:
        self.calls.append(("disable", actor_discord_user_id))
        if self.error is not None:
            raise self.error
        return self.configuration_result


def make_app(*, authorizer: object | None = None) -> tuple[object, FakeRulesService]:
    rules = FakeRulesService()
    control = FakeRulesPublicationControl()
    app = create_app(
        settings(),
        resource_factory=lambda config, read_only: FakeResources(),
        service_factory=lambda factory: FakeAdminService(),
        oauth_client_factory=lambda session, config: SimpleNamespace(),
        bot_profile_control_factory=lambda session, config: control,
        authorization_service_factory=lambda factory, config: (
            authorizer or FakeAuthorizer()
        ),
        rules_service_factory=lambda factory, config: rules,
    )
    app.state.test_rules_publication_control = control
    return app, rules


@contextmanager
def authenticated(
    app: object, user_id: int, role: WebAdminRole
) -> Iterator[tuple[TestClient, str]]:
    issued = app.state.web_session_store.create(user_id, role=role)  # type: ignore[attr-defined]
    with TestClient(app) as client:  # type: ignore[arg-type]
        client.cookies.set(
            SESSION_COOKIE_NAME, issued.session_id, path=SESSION_COOKIE_PATH
        )
        yield client, issued.session.csrf_token


def test_rules_requires_auth_and_allows_owner_and_admin() -> None:
    app, _ = make_app()
    with TestClient(app) as client:  # type: ignore[arg-type]
        anonymous = client.get("/admin/rules", follow_redirects=False)
    assert anonymous.status_code == 303
    for user_id, role in ((41, WebAdminRole.OWNER), (50, WebAdminRole.ADMIN)):
        with authenticated(app, user_id, role) as (client, _):
            assert client.get("/admin/rules").status_code == 200


def test_current_history_counts_and_acceptance_names_are_rendered() -> None:
    app, _ = make_app()
    with authenticated(app, 50, WebAdminRole.ADMIN) as (client, _):
        page = client.get("/admin/rules")
        accepted = client.get("/admin/rules/1/acceptances")

    assert "Текст версии 1.0" in page.text
    assert "Изменения 0.9" in page.text
    assert ">7</a>" in page.text
    assert "Участник" in accepted.text
    assert "<code>100</code>" in accepted.text


def test_publication_section_renders_disabled_current_and_stale_states() -> None:
    app, rules = make_app()
    control = app.state.test_rules_publication_control
    with authenticated(app, 41, WebAdminRole.OWNER) as (client, _):
        disabled = client.get("/admin/rules")
        rules.publication_state = RulesPublicationState(10, 20, 90, 1)
        current = client.get("/admin/rules")
        rules.publication_state = RulesPublicationState(10, 20, 90, 2)
        stale = client.get("/admin/rules")

    assert "Публикация в Discord" in disabled.text
    assert "Отключена" in disabled.text
    assert "Актуально — версия 1.0" in current.text
    assert "#правила" in current.text
    assert "Managed message:</strong> 90" in current.text
    assert "Требуется синхронизация" in stale.text
    assert control.calls.count(("options",)) == 3


def test_compliance_block_renders_summary_and_no_published_state() -> None:
    app, rules = make_app()
    with authenticated(app, 41, WebAdminRole.OWNER) as (client, _):
        summary = client.get("/admin/rules")
        rules.compliance_summary = RulesComplianceSummary(
            RulesComplianceAvailability.NO_PUBLISHED_RULES, 10
        )
        no_published = client.get("/admin/rules")

    assert summary.status_code == 200
    assert "Подтверждение правил" in summary.text
    assert "Текущая версия:</strong> 1.0" in summary.text
    assert "Обязательная точка принятия:</strong> 1.0" in summary.text
    assert "Срок подтверждения:</strong> —" in summary.text
    assert "<strong>7</strong><span>Подтвердили</span>" in summary.text
    assert "<strong>3</strong><span>Ожидают</span>" in summary.text
    assert "<strong>2</strong><span>Просрочили</span>" in summary.text
    assert "<strong>12</strong><span>Всего</span>" in summary.text
    assert "это не обязательно принятие текущей версии" in summary.text
    assert "Нет опубликованных правил" in no_published.text


def test_compliance_summary_failure_does_not_break_rules_or_publication() -> None:
    app, rules = make_app()
    rules.compliance_error = RuntimeError("database read failed")

    with authenticated(app, 50, WebAdminRole.ADMIN) as (client, _):
        response = client.get("/admin/rules")

    assert response.status_code == 200
    assert "Сводку подтверждений временно не удалось загрузить" in response.text
    assert "Публикация в Discord" in response.text
    assert "Правила 1.0" in response.text
    assert "Traceback" not in response.text


def test_configured_channel_survives_options_failure_without_500() -> None:
    app, rules = make_app()
    rules.publication_state = RulesPublicationState(10, 999, 90, 1)
    control = app.state.test_rules_publication_control
    control.options_error = RuntimeError("control unavailable")

    with authenticated(app, 41, WebAdminRole.OWNER) as (client, _):
        response = client.get("/admin/rules")

    assert response.status_code == 200
    assert "ID 999" in response.text
    assert "Bot Control временно недоступен" in response.text
    assert "Traceback" not in response.text


@pytest.mark.parametrize("path", ["channel", "disable", "sync"])
def test_publication_mutations_require_csrf(path: str) -> None:
    app, _ = make_app()
    control = app.state.test_rules_publication_control
    with authenticated(app, 41, WebAdminRole.OWNER) as (client, _):
        response = client.post(
            f"/admin/rules/publication/{path}",
            data={"channel_id": "20"} if path == "channel" else {},
        )

    assert response.status_code == 400
    assert control.calls == []


@pytest.mark.parametrize(
    ("user_id", "role"),
    [(41, WebAdminRole.OWNER), (50, WebAdminRole.ADMIN)],
)
def test_owner_and_admin_can_configure_and_manual_sync(
    user_id: int, role: WebAdminRole
) -> None:
    app, _ = make_app()
    control = app.state.test_rules_publication_control
    control.sync_result = RulesPublicationSyncResult(
        RulesPublicationSyncStatus.CREATED, 10, 20, 90, 1, "1.0"
    )
    with authenticated(app, user_id, role) as (client, csrf):
        configured = client.post(
            "/admin/rules/publication/channel",
            data={"csrf_token": csrf, "channel_id": "20"},
            follow_redirects=False,
        )
        synced = client.post(
            "/admin/rules/publication/sync",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )

    assert configured.status_code == 303
    assert "result=configured" in configured.headers["location"]
    assert "Сообщение с правилами создано" in synced.text
    assert ("configure", 20, user_id) in control.calls
    assert ("sync", user_id) in control.calls


def test_publication_control_unavailable_is_safe_redirect() -> None:
    app, _ = make_app()
    control = app.state.test_rules_publication_control
    control.error = RulesPublicationControlError(
        RulesPublicationControlCategory.CONTROL_UNAVAILABLE
    )
    with authenticated(app, 41, WebAdminRole.OWNER) as (client, csrf):
        response = client.post(
            "/admin/rules/publication/sync",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert "Bot Control временно недоступен" in response.text
    assert "Traceback" not in response.text


def test_admin_can_disable_publication() -> None:
    app, _ = make_app()
    control = app.state.test_rules_publication_control
    control.configuration_result = RulesPublicationConfigurationResult(
        RulesPublicationConfigurationStatus.DISABLED, 10, 20, None, 90
    )
    with authenticated(app, 50, WebAdminRole.ADMIN) as (client, csrf):
        response = client.post(
            "/admin/rules/publication/disable",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "result=disabled" in response.headers["location"]
    assert ("disable", 50) in control.calls


def test_create_copies_published_and_duplicate_is_readable() -> None:
    app, rules = make_app()
    with authenticated(app, 41, WebAdminRole.OWNER) as (client, csrf):
        created = client.post(
            "/admin/rules/drafts",
            data={"csrf_token": csrf, "version": "2.0"},
            follow_redirects=False,
        )
        duplicate = client.post(
            "/admin/rules/drafts",
            data={"csrf_token": csrf, "version": "1.0"},
            follow_redirects=False,
        )

    assert created.status_code == 303
    assert created.headers["location"].startswith("/admin/rules/4")
    assert rules.rules[4].title == rules.rules[1].title
    assert rules.rules[4].content == rules.rules[1].content
    assert "error=duplicate" in duplicate.headers["location"]
    assert rules.mutations[0][0] == "rules.draft_created"


def test_edit_persists_reacceptance_and_published_archived_are_immutable() -> None:
    app, rules = make_app()
    data = {
        "version": "1.2",
        "title": "Новые правила",
        "content": "Новый текст",
        "change_summary": "Важно",
        "requires_reacceptance": "on",
        "reacceptance_grace_days": "14",
    }
    with authenticated(app, 50, WebAdminRole.ADMIN) as (client, csrf):
        saved = client.post("/admin/rules/3/save", data={"csrf_token": csrf, **data})
        published = client.post(
            "/admin/rules/1/save", data={"csrf_token": csrf, **data}
        )
        archived = client.post("/admin/rules/2/save", data={"csrf_token": csrf, **data})

    assert saved.status_code == 200
    assert rules.rules[3].requires_reacceptance is True
    assert rules.rules[3].reacceptance_grace_days == 14
    assert published.status_code == 409
    assert archived.status_code == 409


def test_preview_does_not_mutate_and_csrf_is_required_for_writes() -> None:
    app, rules = make_app()
    data = {
        "version": "9.9",
        "title": "Предпросмотр",
        "content": "Только просмотр",
        "change_summary": "",
    }
    with authenticated(app, 50, WebAdminRole.ADMIN) as (client, csrf):
        preview = client.post(
            "/admin/rules/3/preview", data={"csrf_token": csrf, **data}
        )
        denied = client.post("/admin/rules/3/delete", data={})

    assert preview.status_code == 200
    assert "Предпросмотр Discord" in preview.text
    assert "Только просмотр" in preview.text
    assert rules.rules[3].version == "1.1"
    assert rules.mutations == []
    assert denied.status_code == 400


def test_preview_and_save_explain_discord_embed_limit_failures() -> None:
    app, rules = make_app()
    data = {
        "version": "1.1",
        "title": "T" * 257,
        "content": "Текст",
        "change_summary": "",
    }
    with authenticated(app, 50, WebAdminRole.ADMIN) as (client, csrf):
        preview = client.post(
            "/admin/rules/3/preview", data={"csrf_token": csrf, **data}
        )
        saved = client.post(
            "/admin/rules/3/save",
            data={"csrf_token": csrf, **data},
            follow_redirects=True,
        )

    assert preview.status_code == 422
    assert "Заголовок превышает лимит Discord embed: 256 символов." in preview.text
    assert saved.status_code == 200
    assert "Заголовок превышает лимит Discord embed: 256 символов." in saved.text
    assert rules.rules[3].title != data["title"]
    assert rules.mutations == []


def test_publish_leaves_exactly_one_current_and_delete_only_allows_draft() -> None:
    app, rules = make_app()
    with authenticated(app, 41, WebAdminRole.OWNER) as (client, csrf):
        published = client.post(
            "/admin/rules/3/publish",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        cannot_delete = client.post("/admin/rules/1/delete", data={"csrf_token": csrf})

    assert published.status_code == 303
    assert (
        sum(item.status is RulesetStatus.PUBLISHED for item in rules.rules.values())
        == 1
    )
    assert rules.rules[1].status is RulesetStatus.ARCHIVED
    assert cannot_delete.status_code == 409
    assert ("rules.published", 41) in rules.mutations
    assert ("sync", 41) in app.state.test_rules_publication_control.calls


def test_publish_success_with_sync_failure_keeps_version_and_shows_warning() -> None:
    app, rules = make_app()
    control = app.state.test_rules_publication_control
    control.sync_result = RulesPublicationSyncResult(
        RulesPublicationSyncStatus.FORBIDDEN, 10, 20, 90, 3, "1.1"
    )

    with authenticated(app, 41, WebAdminRole.OWNER) as (client, csrf):
        response = client.post(
            "/admin/rules/3/publish",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert rules.rules[3].status is RulesetStatus.PUBLISHED
    assert "Версия опубликована" in response.text
    assert "сообщение Discord не удалось синхронизировать" in response.text


def test_disabled_publication_does_not_turn_publish_into_failure() -> None:
    app, rules = make_app()
    control = app.state.test_rules_publication_control
    control.sync_result = RulesPublicationSyncResult(
        RulesPublicationSyncStatus.NOT_CONFIGURED, 10
    )

    with authenticated(app, 41, WebAdminRole.OWNER) as (client, csrf):
        response = client.post(
            "/admin/rules/3/publish",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "warning=" not in response.headers["location"]
    assert rules.rules[3].status is RulesetStatus.PUBLISHED


def test_failed_publish_does_not_call_publication_sync() -> None:
    app, _ = make_app()
    control = app.state.test_rules_publication_control

    with authenticated(app, 41, WebAdminRole.OWNER) as (client, csrf):
        response = client.post(
            "/admin/rules/1/publish",
            data={"csrf_token": csrf},
        )

    assert response.status_code == 409
    assert not any(call[0] == "sync" for call in control.calls)


def test_delete_draft_uses_post_and_get_route_does_not_exist() -> None:
    app, rules = make_app()
    with authenticated(app, 50, WebAdminRole.ADMIN) as (client, csrf):
        get_delete = client.get("/admin/rules/3/delete")
        deleted = client.post(
            "/admin/rules/3/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )

    assert get_delete.status_code == 405
    assert deleted.status_code == 303
    assert 3 not in rules.rules
    assert ("rules.draft_deleted", 50) in rules.mutations


class DenyingAuthorizer:
    async def authorize(self, user_id: int) -> WebAdminAuthorizationDecision:
        return WebAdminAuthorizationDecision(
            False, WebAdminAuthorizationCategory.NOT_ALLOWED
        )


def test_existing_session_cannot_use_stale_admin_role_for_rules_reads_or_writes() -> (
    None
):
    app, rules = make_app(authorizer=DenyingAuthorizer())
    with authenticated(app, 41, WebAdminRole.OWNER) as (client, _):
        denied_get = client.get("/admin/rules")
    with authenticated(app, 41, WebAdminRole.OWNER) as (client, csrf):
        denied_post = client.post("/admin/rules/3/delete", data={"csrf_token": csrf})

    assert denied_get.status_code == 403
    assert denied_post.status_code == 403
    assert 3 in rules.rules
    assert rules.mutations == []
    assert app.state.test_rules_publication_control.calls == []


def test_rules_html_escapes_all_user_controlled_values() -> None:
    app, rules = make_app()
    payloads = {
        "version": '<script id="version">alert(1)</script>',
        "title": '<img src=x onerror="title()">',
        "content": '<svg onload="content()"></svg>',
        "change_summary": '<a href="javascript:summary()">change</a>',
    }
    rules.rules[3] = replace(rules.rules[3], **payloads)
    rules.acceptance_name = '<script id="display-name">name()</script>'

    with authenticated(app, 50, WebAdminRole.ADMIN) as (client, csrf):
        index = client.get("/admin/rules")
        detail = client.get("/admin/rules/3")
        acceptances = client.get("/admin/rules/1/acceptances")
        preview = client.post(
            "/admin/rules/3/preview",
            data={"csrf_token": csrf, **payloads},
        )

    combined = "\n".join((index.text, detail.text, acceptances.text, preview.text))
    for raw in payloads.values():
        assert raw not in combined
    assert rules.acceptance_name not in combined
    assert "&lt;script id=&quot;version&quot;&gt;alert(1)&lt;/script&gt;" in combined
    assert "&lt;img src=x onerror=&quot;title()&quot;&gt;" in combined
    assert "&lt;svg onload=&quot;content()&quot;&gt;&lt;/svg&gt;" in combined
    assert "&lt;a href=&quot;javascript:summary()&quot;&gt;change&lt;/a&gt;" in combined
    assert "&lt;script id=&quot;display-name&quot;&gt;name()&lt;/script&gt;" in combined


class TransactionFactory:
    def __init__(self) -> None:
        self.rollbacks = 0

    def begin(self):
        factory = self

        class Transaction:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, exc_type, exc, traceback) -> bool:
                del exc, traceback
                if exc_type is not None:
                    factory.rollbacks += 1
                return False

        return Transaction()


class ConstraintViolation(Exception):
    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constraint_name", "expected_error"),
    [
        ("uq_rulesets_guild_version", DuplicateRulesetVersionError),
        ("fk_rulesets_created_by_discord_users", IntegrityError),
    ],
)
async def test_integrity_error_mapping_is_limited_to_version_constraint(
    monkeypatch: pytest.MonkeyPatch,
    constraint_name: str,
    expected_error: type[Exception],
) -> None:
    transaction_factory = TransactionFactory()
    integrity_error = IntegrityError("INSERT", {}, ConstraintViolation(constraint_name))

    class FailingRulesService:
        async def create_draft(self, *args: object, **kwargs: object) -> RulesetRecord:
            raise integrity_error

    monkeypatch.setattr(
        web_rules_module, "SqlAlchemyRulesRepository", lambda session: object()
    )
    monkeypatch.setattr(
        web_rules_module, "RulesService", lambda repository: FailingRulesService()
    )
    service = WebAdminRulesService(transaction_factory, guild_id=10)  # type: ignore[arg-type]

    with pytest.raises(expected_error) as captured:
        await service.create_draft("1.1", 41)

    if expected_error is IntegrityError:
        assert captured.value is integrity_error
    assert transaction_factory.rollbacks == 1


@pytest.mark.asyncio
async def test_concurrent_delete_conflict_does_not_create_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_factory = TransactionFactory()

    class ConcurrentRulesService:
        async def delete_draft(self, guild_id: int, ruleset_id: int) -> RulesetRecord:
            raise ImmutableRulesetError

    monkeypatch.setattr(
        web_rules_module, "SqlAlchemyRulesRepository", lambda session: object()
    )
    monkeypatch.setattr(
        web_rules_module, "RulesService", lambda repository: ConcurrentRulesService()
    )
    service = WebAdminRulesService(transaction_factory, guild_id=10)  # type: ignore[arg-type]
    audit = AsyncMock()
    monkeypatch.setattr(service, "_audit", audit)

    with pytest.raises(ImmutableRulesetError):
        await service.delete_draft(3, 41)

    audit.assert_not_awaited()
    assert transaction_factory.rollbacks == 1
