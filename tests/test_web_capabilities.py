import re
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Iterator

import pytest
from starlette.testclient import TestClient

from discord_stats_bot.config import MAX_DISCORD_SNOWFLAKE, WebSettings
from discord_stats_bot.features.capabilities import (
    VOICE_MOVE,
    CapabilityGrant,
    CapabilityPresentationSubjects,
    CapabilitySubjectType,
    GuildCapabilityPolicy,
    get_capability,
)
from discord_stats_bot.web.app import create_app
from discord_stats_bot.web.auth import SESSION_COOKIE_NAME, SESSION_COOKIE_PATH
from discord_stats_bot.web.authorization import (
    WebAdminAuthorizationCategory,
    WebAdminAuthorizationDecision,
    WebAdminRole,
)
from discord_stats_bot.web.capabilities import (
    WebAdminCapabilitiesReadService,
    WebAdminCapabilityGrant,
    WebAdminCapabilityMemberOption,
    WebAdminCapabilityRoleOption,
    WebAdminCapabilitySubject,
    WebAdminCapabilityView,
    render_capabilities_page,
)
from discord_stats_bot.web.service import (
    AdminCounts,
    AdminMemberDetailResult,
    AdminMemberDetailStatus,
    AdminMembersPage,
    WebDatabaseHealth,
)


def settings() -> WebSettings:
    return WebSettings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://test:test@localhost:5432/test",
        DISCORD_GUILD_ID=10,
        WEB_ADMIN_DISCORD_CLIENT_ID=123,
        WEB_ADMIN_DISCORD_CLIENT_SECRET="oauth-secret",
        WEB_ADMIN_DISCORD_REDIRECT_URI=(
            "http://localhost:8000/admin/auth/discord/callback"
        ),
        WEB_ADMIN_COOKIE_SECURE=False,
        WEB_ADMIN_ALLOWED_USER_IDS="41",
    )


class Resources:
    session_factory = object()

    async def dispose(self) -> None:
        pass


class AdminService:
    async def probe_database(self) -> WebDatabaseHealth:
        return WebDatabaseHealth(True, 0.01)

    async def load_counts(self) -> AdminCounts:
        return AdminCounts(0, 0, 0)

    async def load_members(self, **kwargs: object) -> AdminMembersPage:
        return AdminMembersPage((), 0, 1, 50, "")

    async def load_member_detail(self, user_id: int) -> AdminMemberDetailResult:
        return AdminMemberDetailResult(AdminMemberDetailStatus.NOT_FOUND)


class Authorizer:
    def __init__(self) -> None:
        self.roles = {41: WebAdminRole.OWNER, 50: WebAdminRole.ADMIN}

    async def authorize(self, user_id: int) -> WebAdminAuthorizationDecision:
        role = self.roles.get(user_id)
        if role is None:
            return WebAdminAuthorizationDecision(
                False, WebAdminAuthorizationCategory.NOT_ALLOWED
            )
        return WebAdminAuthorizationDecision(True, role=role)


class StaticCapabilities:
    def __init__(self, result: tuple[WebAdminCapabilityView, ...]) -> None:
        self.result = result
        self.role_validation_calls: list[int] = []
        self.member_validation_calls: list[int] = []
        self.member_options_available = True

    async def load(self) -> tuple[WebAdminCapabilityView, ...]:
        return self.result

    async def is_current_role_option(self, role_id: int) -> bool:
        self.role_validation_calls.append(role_id)
        return role_id in {70, 71}

    async def is_current_user_option(self, user_id: int) -> bool:
        self.member_validation_calls.append(user_id)
        if not self.member_options_available:
            raise RuntimeError("member options unavailable")
        return user_id in {80, 81}


class MutationService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | bool, int]] = []
        self.user_calls: list[tuple[str, str, int, int]] = []
        self.changed = True

    async def set_enabled(
        self, capability: str, enabled: bool, actor_user_id: int
    ) -> bool:
        self.calls.append(("policy", capability, enabled, actor_user_id))
        return self.changed

    async def grant_role(
        self, capability: str, role_id: int, actor_user_id: int
    ) -> bool:
        self.calls.append(("grant", capability, role_id, actor_user_id))
        return self.changed

    async def revoke_role(
        self, capability: str, role_id: int, actor_user_id: int
    ) -> bool:
        self.calls.append(("revoke", capability, role_id, actor_user_id))
        return self.changed

    async def grant_user(
        self, capability: str, user_id: int, actor_user_id: int
    ) -> bool:
        self.user_calls.append(("grant_user", capability, user_id, actor_user_id))
        return self.changed

    async def revoke_user(
        self, capability: str, user_id: int, actor_user_id: int
    ) -> bool:
        self.user_calls.append(("revoke_user", capability, user_id, actor_user_id))
        return self.changed


class RoleOptionsControl:
    async def get_capability_role_options(self) -> tuple[tuple[int, str], ...]:
        return ((70, "Moderators"), (71, "Voice team"))


def grant(
    label: str, subject_id: int, *, missing: bool = False
) -> WebAdminCapabilityGrant:
    return WebAdminCapabilityGrant(
        WebAdminCapabilitySubject(label, subject_id, missing)
    )


def capability(*, enabled: bool = False) -> WebAdminCapabilityView:
    definition = get_capability(VOICE_MOVE)
    return WebAdminCapabilityView(
        definition,
        enabled,
        (grant("Moderators", 70),),
        (grant("Display member", 80),),
    )


def make_app(result: tuple[WebAdminCapabilityView, ...] | None = None):
    authorizer = Authorizer()
    service = StaticCapabilities(result or (capability(),))
    mutation = MutationService()
    app = create_app(
        settings(),
        resource_factory=lambda settings, read_only: Resources(),
        service_factory=lambda session_factory: AdminService(),
        oauth_client_factory=lambda session, settings: SimpleNamespace(),
        bot_profile_control_factory=lambda session, settings: SimpleNamespace(),
        authorization_service_factory=lambda session, settings: authorizer,
        capabilities_read_service_factory=lambda session, settings, control: service,
        capabilities_mutation_service_factory=lambda session, settings: mutation,
    )
    app.state.test_capabilities_service = service
    return app, authorizer, mutation


@contextmanager
def authenticated(
    app: object, user_id: int, role: WebAdminRole
) -> Iterator[TestClient]:
    issued = app.state.web_session_store.create(user_id, role=role)  # type: ignore[attr-defined]
    with TestClient(app) as client:  # type: ignore[arg-type]
        client.cookies.set(
            SESSION_COOKIE_NAME, issued.session_id, path=SESSION_COOKIE_PATH
        )
        yield client


def csrf_token(client: TestClient) -> str:
    response = client.get("/admin/capabilities")
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def test_capability_page_shows_registry_presentation_and_default_disabled() -> None:
    app, _, _ = make_app()
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        response = client.get("/admin/capabilities")

    assert response.status_code == 200
    assert "Voice moderation" in response.text
    assert "Move members" in response.text
    assert "Управляемое право Kanami" in response.text
    assert ">Disabled</span>" in response.text
    assert "Allowed roles" in response.text
    assert "Allowed users" in response.text
    assert 'href="/admin/capabilities"' in response.text


def test_capability_page_shows_enabled_and_human_readable_grants() -> None:
    view = capability(enabled=True)
    view = WebAdminCapabilityView(
        view.definition,
        view.enabled,
        (grant("Moderators", 70), grant("Voice team", 71)),
        (grant("Display member", 80), grant("Another member", 81)),
    )
    app, _, _ = make_app((view,))
    with authenticated(app, 50, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/capabilities")

    assert ">Enabled</span>" in response.text
    for label in ("Moderators", "Voice team", "Display member", "Another member"):
        assert label in response.text
    role_item = response.text.split("Moderators", 1)[1].split("</li>", 1)[0]
    assert not role_item.startswith("70")


def test_missing_entities_use_safe_fallbacks_and_empty_states() -> None:
    view = capability()
    view = WebAdminCapabilityView(
        view.definition,
        False,
        (grant("Unknown role", 70, missing=True),),
        (grant("Unknown member", 80, missing=True),),
    )
    app, _, _ = make_app((view,))
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        response = client.get("/admin/capabilities")

    assert response.status_code == 200
    assert "Unknown role" in response.text
    assert "Unknown member" in response.text
    assert "Discord ID · 70" in response.text
    assert "Discord ID · 80" in response.text


def test_capability_page_shows_empty_grant_states() -> None:
    view = capability()
    app, _, _ = make_app((WebAdminCapabilityView(view.definition, False, (), ()),))
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        response = client.get("/admin/capabilities")

    assert "Нет разрешённых ролей." in response.text
    assert "Нет разрешённых пользователей." in response.text


def test_capability_page_uses_existing_web_admin_auth_policy() -> None:
    app, authorizer, _ = make_app()
    with TestClient(app) as client:
        anonymous = client.get("/admin/capabilities", follow_redirects=False)
    authorizer.roles.pop(50)
    with authenticated(app, 50, WebAdminRole.ADMIN) as client:
        denied = client.get("/admin/capabilities")

    assert anonymous.status_code == 303
    assert denied.status_code == 403


@pytest.mark.parametrize(
    "user_id,role", [(41, WebAdminRole.OWNER), (50, WebAdminRole.ADMIN)]
)
def test_authorized_web_admin_can_enable_capability(
    user_id: int, role: WebAdminRole
) -> None:
    app, _, mutation = make_app()
    with authenticated(app, user_id, role) as client:
        response = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "voice.move",
                "enabled": "true",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].endswith("result=enabled")
    assert mutation.calls == [("policy", "voice.move", True, user_id)]


def test_capability_post_rejects_invalid_csrf_form_and_unknown_key() -> None:
    app, _, mutation = make_app()
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        missing_csrf = client.post("/admin/capabilities", data={})
        wrong_content = client.post(
            "/admin/capabilities", content=b"x", headers={"content-type": "text/plain"}
        )
        duplicate = client.post(
            "/admin/capabilities",
            content=(
                f"csrf_token={csrf_token(client)}&capability=voice.move&"
                "capability=voice.move&enabled=true"
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        unknown = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "future.capability",
                "enabled": "true",
            },
            follow_redirects=False,
        )

    assert [item.status_code for item in (missing_csrf, wrong_content, duplicate)] == [
        400,
        400,
        400,
    ]
    assert unknown.status_code == 303
    assert unknown.headers["location"].endswith("result=invalid")
    assert mutation.calls == []


def test_capability_post_noop_and_get_never_mutates() -> None:
    app, _, mutation = make_app()
    mutation.changed = False
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        before = client.get("/admin/capabilities")
        no_op = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "voice.move",
                "enabled": "false",
            },
            follow_redirects=False,
        )

    assert before.status_code == 200
    assert no_op.headers["location"].endswith("result=already_disabled")
    assert mutation.calls == [("policy", "voice.move", False, 41)]


def test_capability_page_offers_role_names_and_excludes_granted_role() -> None:
    view = capability()
    view = WebAdminCapabilityView(
        view.definition,
        False,
        view.role_grants,
        view.user_grants,
        role_options=(
            # The current grant must never appear as a repeat add option.
            WebAdminCapabilityRoleOption(71, "Voice team"),
        ),
        role_options_available=True,
    )
    app, _, _ = make_app((view,))
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        response = client.get("/admin/capabilities")
    assert 'option value="71">Voice team' in response.text
    assert 'option value="70">Moderators' not in response.text
    assert "Remove Moderators" in response.text


def test_role_grant_and_revoke_use_prg_and_validate_live_options() -> None:
    app, _, mutation = make_app()
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        token = csrf_token(client)
        granted = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": token,
                "capability": "voice.move",
                "role_id": "71",
                "operation": "grant",
            },
            follow_redirects=False,
        )
        forged = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "voice.move",
                "role_id": "72",
                "operation": "grant",
            },
            follow_redirects=False,
        )
        revoked = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "voice.move",
                "role_id": "999",
                "operation": "revoke",
            },
            follow_redirects=False,
        )
    assert granted.headers["location"].endswith("result=granted")
    assert forged.headers["location"].endswith("result=invalid_role")
    assert revoked.headers["location"].endswith("result=revoked")
    assert mutation.calls == [
        ("grant", "voice.move", 71, 41),
        ("revoke", "voice.move", 999, 41),
    ]


def test_role_grant_rejections_do_not_validate_or_mutate_before_required_checks() -> (
    None
):
    app, _, mutation = make_app()
    service = app.state.test_capabilities_service
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        csrf = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": "wrong",
                "capability": "voice.move",
                "role_id": "71",
                "operation": "grant",
            },
        )
        unknown = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "unknown",
                "role_id": "71",
                "operation": "grant",
            },
            follow_redirects=False,
        )
    assert csrf.status_code == 400
    assert unknown.headers["location"].endswith("result=invalid")
    assert service.role_validation_calls == []
    assert mutation.calls == []


@pytest.mark.parametrize(
    "user_id,role", [(41, WebAdminRole.OWNER), (50, WebAdminRole.ADMIN)]
)
def test_authorized_web_admin_can_grant_current_member(
    user_id: int, role: WebAdminRole
) -> None:
    app, _, mutation = make_app()
    with authenticated(app, user_id, role) as client:
        response = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "voice.move",
                "user_id": "81",
                "operation": "grant_user",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].endswith("result=user_granted")
    assert mutation.user_calls == [("grant_user", "voice.move", 81, user_id)]


def test_user_grant_rejection_and_outage_do_not_mutate() -> None:
    app, _, mutation = make_app()
    service = app.state.test_capabilities_service
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        forged = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "voice.move",
                "user_id": "999",
                "operation": "grant_user",
            },
            follow_redirects=False,
        )
        bot_target = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "voice.move",
                "user_id": "82",
                "operation": "grant_user",
            },
            follow_redirects=False,
        )
        service.member_options_available = False
        outage = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "voice.move",
                "user_id": "81",
                "operation": "grant_user",
            },
            follow_redirects=False,
        )

    assert forged.headers["location"].endswith("result=invalid_member")
    assert bot_target.headers["location"].endswith("result=invalid_member")
    assert outage.headers["location"].endswith("result=member_options_unavailable")
    assert service.member_validation_calls == [999, 82, 81]
    assert mutation.user_calls == []


def test_user_grant_noop_and_unknown_capability() -> None:
    app, _, mutation = make_app()
    mutation.changed = False
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        duplicate = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "voice.move",
                "user_id": "81",
                "operation": "grant_user",
            },
            follow_redirects=False,
        )
        unknown = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "unknown",
                "user_id": "81",
                "operation": "grant_user",
            },
            follow_redirects=False,
        )

    assert duplicate.headers["location"].endswith("result=user_already_granted")
    assert unknown.headers["location"].endswith("result=invalid")
    assert mutation.user_calls == [("grant_user", "voice.move", 81, 41)]


def test_user_revoke_skips_live_validation_and_supports_noop() -> None:
    app, _, mutation = make_app()
    service = app.state.test_capabilities_service
    service.member_options_available = False
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        revoked = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "voice.move",
                "user_id": "999",
                "operation": "revoke_user",
            },
            follow_redirects=False,
        )
        mutation.changed = False
        repeated = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": csrf_token(client),
                "capability": "voice.move",
                "user_id": "999",
                "operation": "revoke_user",
            },
            follow_redirects=False,
        )

    assert revoked.status_code == repeated.status_code == 303
    assert revoked.headers["location"].endswith("result=user_revoked")
    assert repeated.headers["location"].endswith("result=user_not_granted")
    assert service.member_validation_calls == []
    assert mutation.user_calls == [
        ("revoke_user", "voice.move", 999, 41),
        ("revoke_user", "voice.move", 999, 41),
    ]


def test_get_capabilities_never_mutates_user_grants() -> None:
    app, _, mutation = make_app()
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        response = client.get("/admin/capabilities")

    assert response.status_code == 200
    assert mutation.user_calls == []


def test_user_controls_and_notices_cover_stale_and_disabled_grants() -> None:
    view = WebAdminCapabilityView(
        get_capability(VOICE_MOVE),
        False,
        (),
        (grant("Unknown member", 80, missing=True),),
        member_options=(WebAdminCapabilityMemberOption(81, "Bob"),),
        member_options_available=True,
    )
    page = render_capabilities_page((view,), csrf_token="csrf", role=WebAdminRole.OWNER)

    assert ">Disabled</span>" in page
    assert 'option value="81">Bob' in page
    assert 'name="user_id" value="80"' in page
    assert 'name="operation" value="revoke_user"' in page
    assert "Remove Unknown member" in page
    assert "User granted." in render_capabilities_page(
        (view,), csrf_token="csrf", role=WebAdminRole.OWNER, result="user_granted"
    )
    for result, message in (
        ("user_already_granted", "User is already granted."),
        ("user_revoked", "User revoked."),
        ("user_not_granted", "User grant is already removed."),
        ("invalid_member", "Invalid member."),
        (
            "member_options_unavailable",
            "Discord member options are temporarily unavailable.",
        ),
    ):
        rendered = render_capabilities_page(
            (view,), csrf_token="csrf", role=WebAdminRole.OWNER, result=result
        )
        assert message in rendered
        assert ("notice failure" in rendered) is (
            result
            in {
                "invalid_member",
                "member_options_unavailable",
            }
        )


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {
            "csrf_token": "wrong",
            "capability": "voice.move",
            "user_id": "81",
            "operation": "grant_user",
        },
        {
            "csrf_token": "csrf",
            "capability": "voice.move",
            "user_id": "",
            "operation": "grant_user",
        },
        {
            "csrf_token": "csrf",
            "capability": "voice.move",
            "user_id": "0",
            "operation": "grant_user",
        },
        {
            "csrf_token": "csrf",
            "capability": "voice.move",
            "user_id": str(MAX_DISCORD_SNOWFLAKE + 1),
            "operation": "grant_user",
        },
        {
            "csrf_token": "csrf",
            "capability": "voice.move",
            "user_id": "abc",
            "operation": "grant_user",
        },
        {
            "csrf_token": "csrf",
            "capability": "voice.move",
            "user_id": "81",
            "operation": "unsupported",
        },
    ),
)
def test_user_invalid_requests_do_not_validate_or_mutate(
    payload: dict[str, str],
) -> None:
    app, _, mutation = make_app()
    service = app.state.test_capabilities_service
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        values = {**payload}
        if values.get("csrf_token") == "csrf":
            values["csrf_token"] = csrf_token(client)
        response = client.post(
            "/admin/capabilities", data=values, follow_redirects=False
        )

    assert response.status_code in {303, 400}
    assert service.member_validation_calls == []
    assert mutation.user_calls == []


def test_user_form_contract_rejects_bad_shapes_and_preserves_denial_headers() -> None:
    app, _, mutation = make_app()
    service = app.state.test_capabilities_service
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        token = csrf_token(client)
        wrong_content = client.post(
            "/admin/capabilities", content=b"x", headers={"content-type": "text/plain"}
        )
        duplicate = client.post(
            "/admin/capabilities",
            content=(
                f"csrf_token={token}&capability=voice.move&user_id=81&"
                "user_id=81&operation=grant_user"
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        unexpected = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": token,
                "capability": "voice.move",
                "user_id": "81",
                "operation": "grant_user",
                "extra": "x",
            },
        )
        mixed_role = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": token,
                "capability": "voice.move",
                "role_id": "70",
                "user_id": "81",
                "operation": "grant_user",
            },
        )
        mixed_policy = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": token,
                "capability": "voice.move",
                "enabled": "true",
                "user_id": "81",
                "operation": "grant_user",
            },
        )
        oversized = client.post(
            "/admin/capabilities",
            content=(
                f"csrf_token={token}&capability=voice.move&" + "x" * 4096
            ).encode(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    for response in (
        wrong_content,
        duplicate,
        unexpected,
        mixed_role,
        mixed_policy,
        oversized,
    ):
        assert response.status_code == 400
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert service.member_validation_calls == []
    assert mutation.user_calls == []


def test_user_authorization_and_rate_limit_are_enforced() -> None:
    app, authorizer, mutation = make_app()
    service = app.state.test_capabilities_service
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        token = csrf_token(client)
        authorizer.roles.pop(41)
        denied = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": token,
                "capability": "voice.move",
                "user_id": "81",
                "operation": "grant_user",
            },
        )

    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store"
    assert denied.headers["referrer-policy"] == "no-referrer"
    assert service.member_validation_calls == []
    assert mutation.user_calls == []

    app, _, mutation = make_app()
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        token = csrf_token(client)
        for _ in range(10):
            response = client.post(
                "/admin/capabilities",
                data={
                    "csrf_token": token,
                    "capability": "voice.move",
                    "user_id": "81",
                    "operation": "revoke_user",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
        limited = client.post(
            "/admin/capabilities",
            data={
                "csrf_token": token,
                "capability": "voice.move",
                "user_id": "81",
                "operation": "revoke_user",
            },
        )

    assert limited.status_code == 429
    assert limited.headers["cache-control"] == "no-store"
    assert limited.headers["referrer-policy"] == "no-referrer"
    assert len(mutation.user_calls) == 10


class SubjectControl:
    async def get_capability_presentation_subjects(
        self, role_ids: tuple[int, ...], user_ids: tuple[int, ...]
    ) -> CapabilityPresentationSubjects:
        assert role_ids == (70,)
        assert user_ids == (80,)
        return CapabilityPresentationSubjects(((70, "Moderators"),), ((80, "Member"),))

    async def get_capability_role_options(self) -> tuple[tuple[int, str], ...]:
        return ((70, "Moderators"), (71, "Voice team"))

    async def get_capability_member_options(self) -> tuple[tuple[int, str], ...]:
        return ((80, "Member"), (81, "Bob"))


class Repository:
    calls: list[str] = []
    policy: GuildCapabilityPolicy | None = None
    grants: tuple[CapabilityGrant, ...] = ()

    def __init__(self, session: object) -> None:
        del session

    async def get_policy(self, guild_id: int, key: str):
        self.calls.append(key)
        return self.policy

    async def list_grants(self, guild_id: int, key: str):
        self.calls.append(key)
        return self.grants


class SessionFactory:
    def __call__(self):
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        pass


@pytest.mark.asyncio
async def test_read_service_uses_registry_default_and_ignores_unknown_rows(
    monkeypatch,
) -> None:
    Repository.calls = []
    Repository.policy = None
    now = datetime.now(UTC)
    Repository.grants = (
        CapabilityGrant(10, VOICE_MOVE, CapabilitySubjectType.ROLE, 70, now, 1),
        CapabilityGrant(10, VOICE_MOVE, CapabilitySubjectType.USER, 80, now, 1),
    )
    monkeypatch.setattr(
        "discord_stats_bot.web.capabilities.SqlAlchemyCapabilityRepository", Repository
    )
    loaded = await WebAdminCapabilitiesReadService(
        SessionFactory(),
        SubjectControl(),
        guild_id=10,  # type: ignore[arg-type]
    ).load()

    assert loaded is not None
    assert [item.definition.key for item in loaded] == [VOICE_MOVE]
    assert loaded[0].enabled is False
    assert loaded[0].role_grants[0].subject.label == "Moderators"
    assert loaded[0].user_grants[0].subject.label == "Member"
    assert loaded[0].role_options == (WebAdminCapabilityRoleOption(71, "Voice team"),)
    assert loaded[0].role_options_available is True
    assert loaded[0].member_options == (WebAdminCapabilityMemberOption(81, "Bob"),)
    assert loaded[0].member_options_available is True
    assert Repository.calls == [VOICE_MOVE, VOICE_MOVE]


@pytest.mark.asyncio
async def test_read_service_uses_existing_enabled_policy_record(monkeypatch) -> None:
    Repository.calls = []
    Repository.policy = GuildCapabilityPolicy(
        10, VOICE_MOVE, True, datetime.now(UTC), 1
    )
    monkeypatch.setattr(
        "discord_stats_bot.web.capabilities.SqlAlchemyCapabilityRepository", Repository
    )
    loaded = await WebAdminCapabilitiesReadService(
        SessionFactory(),
        SubjectControl(),
        guild_id=10,  # type: ignore[arg-type]
    ).load()

    assert loaded is not None
    assert loaded[0].enabled is True


class PartialSubjectControl:
    async def get_capability_presentation_subjects(
        self, role_ids: tuple[int, ...], user_ids: tuple[int, ...]
    ) -> CapabilityPresentationSubjects:
        return CapabilityPresentationSubjects(((70, "Moderators"),), ())

    async def get_capability_role_options(self) -> tuple[tuple[int, str], ...]:
        return ((70, "Moderators"),)

    async def get_capability_member_options(self) -> tuple[tuple[int, str], ...]:
        return ((81, "Bob"),)


class UnavailableSubjectControl:
    async def get_capability_presentation_subjects(
        self, role_ids: tuple[int, ...], user_ids: tuple[int, ...]
    ) -> CapabilityPresentationSubjects:
        raise RuntimeError("control unavailable")

    async def get_capability_role_options(self) -> tuple[tuple[int, str], ...]:
        raise RuntimeError("control unavailable")

    async def get_capability_member_options(self) -> tuple[tuple[int, str], ...]:
        return ((81, "Bob"),)


@pytest.mark.asyncio
async def test_successful_resolution_marks_only_absent_entity_missing(
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    Repository.policy = None
    Repository.grants = (
        CapabilityGrant(10, VOICE_MOVE, CapabilitySubjectType.ROLE, 70, now, 1),
        CapabilityGrant(10, VOICE_MOVE, CapabilitySubjectType.ROLE, 71, now, 1),
    )
    monkeypatch.setattr(
        "discord_stats_bot.web.capabilities.SqlAlchemyCapabilityRepository", Repository
    )
    loaded = await WebAdminCapabilitiesReadService(
        SessionFactory(),
        PartialSubjectControl(),
        guild_id=10,  # type: ignore[arg-type]
    ).load()

    assert loaded is not None
    known, stale = (grant.subject for grant in loaded[0].role_grants)
    assert (known.label, known.missing) == ("Moderators", False)
    assert (stale.label, stale.missing) == ("Unknown role", True)


@pytest.mark.asyncio
async def test_subject_lookup_outage_keeps_db_state_without_marking_entities_missing(
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    Repository.policy = GuildCapabilityPolicy(10, VOICE_MOVE, True, now, 1)
    Repository.grants = (
        CapabilityGrant(10, VOICE_MOVE, CapabilitySubjectType.ROLE, 70, now, 1),
        CapabilityGrant(10, VOICE_MOVE, CapabilitySubjectType.USER, 80, now, 1),
    )
    monkeypatch.setattr(
        "discord_stats_bot.web.capabilities.SqlAlchemyCapabilityRepository", Repository
    )
    loaded = await WebAdminCapabilitiesReadService(
        SessionFactory(),
        UnavailableSubjectControl(),
        guild_id=10,  # type: ignore[arg-type]
    ).load()

    assert loaded is not None
    view = loaded[0]
    assert view.enabled is True
    assert view.subject_resolution_available is False
    assert view.role_options_available is False
    assert view.member_options_available is True
    assert [
        (item.subject.label, item.subject.missing) for item in view.role_grants
    ] == [("Role name unavailable", False)]
    assert [
        (item.subject.label, item.subject.missing) for item in view.user_grants
    ] == [("Member name unavailable", False)]
    page = render_capabilities_page(loaded, csrf_token="csrf", role=WebAdminRole.OWNER)
    assert "Discord role/member names temporarily unavailable." in page
    assert ">Enabled</span>" in page


class MemberOptionsUnavailableControl(SubjectControl):
    async def get_capability_member_options(self) -> tuple[tuple[int, str], ...]:
        raise RuntimeError("member options unavailable")


@pytest.mark.asyncio
async def test_member_options_outage_is_independent_of_read_model_state(
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    Repository.policy = GuildCapabilityPolicy(10, VOICE_MOVE, True, now, 1)
    Repository.grants = (
        CapabilityGrant(10, VOICE_MOVE, CapabilitySubjectType.ROLE, 70, now, 1),
        CapabilityGrant(10, VOICE_MOVE, CapabilitySubjectType.USER, 80, now, 1),
    )
    monkeypatch.setattr(
        "discord_stats_bot.web.capabilities.SqlAlchemyCapabilityRepository", Repository
    )

    loaded = await WebAdminCapabilitiesReadService(
        SessionFactory(),
        MemberOptionsUnavailableControl(),
        guild_id=10,  # type: ignore[arg-type]
    ).load()

    assert loaded is not None
    view = loaded[0]
    assert view.enabled is True
    assert view.role_grants[0].subject.label == "Moderators"
    assert view.user_grants[0].subject.label == "Member"
    assert view.role_options == (WebAdminCapabilityRoleOption(71, "Voice team"),)
    assert view.role_options_available is True
    assert view.member_options == ()
    assert view.member_options_available is False


@pytest.mark.asyncio
async def test_stale_user_subject_is_unknown_and_remove_control_remains(
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    Repository.policy = None
    Repository.grants = (
        CapabilityGrant(10, VOICE_MOVE, CapabilitySubjectType.USER, 80, now, 1),
    )
    monkeypatch.setattr(
        "discord_stats_bot.web.capabilities.SqlAlchemyCapabilityRepository", Repository
    )

    loaded = await WebAdminCapabilitiesReadService(
        SessionFactory(),
        PartialSubjectControl(),
        guild_id=10,  # type: ignore[arg-type]
    ).load()

    assert loaded is not None
    subject = loaded[0].user_grants[0].subject
    assert (subject.label, subject.missing) == ("Unknown member", True)
    page = render_capabilities_page(loaded, csrf_token="csrf", role=WebAdminRole.OWNER)
    assert 'name="user_id" value="80"' in page
    assert 'name="operation" value="revoke_user"' in page


def test_role_mutation_denial_response_has_no_store_headers() -> None:
    app, _, mutation = make_app()
    with authenticated(app, 41, WebAdminRole.OWNER) as client:
        response = client.post("/admin/capabilities", data={})
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert mutation.calls == []
