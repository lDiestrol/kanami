from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.discord import MemberProfileCommandHandler
from discord_stats_bot.discord.member_profile import format_profile_voice_duration
from discord_stats_bot.features.achievements import UnlockedAchievement
from discord_stats_bot.features.member_profile import (
    KanamiMemberRole,
    MemberProfileService,
    MemberProfileSubject,
    MemberRoleConfiguration,
    can_view_member_statistics,
    resolve_kanami_member_role,
)
from discord_stats_bot.features.voice_statistics import VoiceStatistics
from tests.support.discord import make_interaction, make_member
from tests.support.persistence import FakeSessionFactory
from tests.support.voice import make_voice_statistics

AS_OF = datetime(2026, 8, 24, 12, tzinfo=UTC)
ROLES = MemberRoleConfiguration(
    guest_role_id=101,
    initiated_role_id=102,
    guardian_role_id=103,
    purple_role_id=104,
    gold_role_id=105,
)


class RecordingVoiceRepository:
    def __init__(
        self,
        *,
        all_time_seconds: int = 638 * 3600 + 24 * 60,
        last_30_days_seconds: int = 47 * 3600 + 12 * 60,
    ) -> None:
        self.statistics = make_voice_statistics(
            as_of=AS_OF,
            all_time_seconds=all_time_seconds,
            last_30_days_seconds=last_30_days_seconds,
        )
        self.calls: list[tuple[int, int, object]] = []

    async def get_user_statistics(
        self, guild_id: int, user_id: int, query: object
    ) -> VoiceStatistics:
        self.calls.append((guild_id, user_id, query))
        return self.statistics


class RecordingAchievementRepository:
    def __init__(self, count: int = 7) -> None:
        self.records = tuple(
            UnlockedAchievement(10, 20, f"achievement_{index}", AS_OF)
            for index in range(count)
        )
        self.calls: list[tuple[int, int]] = []

    async def list_unlocked(
        self, *, guild_id: int, user_id: int
    ) -> tuple[UnlockedAchievement, ...]:
        self.calls.append((guild_id, user_id))
        return self.records


def make_handler(
    *,
    roles: MemberRoleConfiguration = ROLES,
    voice: RecordingVoiceRepository | None = None,
    achievements: RecordingAchievementRepository | None = None,
) -> tuple[
    MemberProfileCommandHandler,
    FakeSessionFactory,
    RecordingVoiceRepository,
    RecordingAchievementRepository,
]:
    sessions = FakeSessionFactory()
    voice = voice or RecordingVoiceRepository()
    achievements = achievements or RecordingAchievementRepository()
    handler = MemberProfileCommandHandler(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
        min_session_seconds=10,
        role_configuration=roles,
        voice_repository_factory=lambda session: voice,  # type: ignore[arg-type]
        achievement_repository_factory=lambda session: achievements,
        clock=lambda: AS_OF,
    )
    return handler, sessions, voice, achievements


@pytest.mark.asyncio
async def test_profile_defaults_to_invoker_and_renders_complete_card() -> None:
    handler, sessions, voice, achievements = make_handler()
    member = make_member(
        20,
        display_name="D4rki",
        joined_at=AS_OF - timedelta(days=821, hours=2),
        avatar_url="https://cdn.example/avatar.png",
        role_ids=(103,),
    )
    interaction = make_interaction(user=member)

    await handler.handle(interaction)  # type: ignore[arg-type]

    assert voice.calls[0][:2] == (10, 20)
    assert achievements.calls == [(10, 20)]
    assert sessions.calls == 1
    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    kwargs = interaction.followup.messages[0][1]
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert embed.description == "D4rki\nСтраж"
    assert embed.thumbnail.url == "https://cdn.example/avatar.png"
    fields = {field.name: field.value for field in embed.fields}
    assert "821 дн. на сервере" in fields["На сервере"]
    assert "Всего: 638 ч 24 мин" in fields["Voice"]
    assert "За 30 дней: 47 ч 12 мин" in fields["Voice"]
    assert fields["Достижения"] == "Получено: 7"


@pytest.mark.asyncio
async def test_explicit_user_is_profile_subject_for_privileged_viewer() -> None:
    handler, _, voice, achievements = make_handler()
    viewer = make_member(20, role_ids=(104,))
    target = make_member(21, display_name="Target", role_ids=(101,))
    interaction = make_interaction(user=viewer)

    await handler.handle(interaction, target)  # type: ignore[arg-type]

    assert voice.calls[0][:2] == (10, 21)
    assert achievements.calls == [(10, 21)]
    assert interaction.followup.messages[0][1]["embed"].description == "Target\nГость"


@pytest.mark.asyncio
async def test_ordinary_member_can_view_self_without_role_configuration() -> None:
    handler, sessions, _, _ = make_handler(roles=MemberRoleConfiguration())

    await handler.handle(make_interaction(user=make_member(20)))  # type: ignore[arg-type]

    assert sessions.calls == 1


@pytest.mark.parametrize("role_id", [101, 102, 103])
@pytest.mark.asyncio
async def test_regular_levels_cannot_view_another_member(role_id: int) -> None:
    handler, sessions, _, _ = make_handler()
    interaction = make_interaction(user=make_member(20, role_ids=(role_id,)))

    await handler.handle(interaction, make_member(21))  # type: ignore[arg-type]

    assert sessions.calls == 0
    assert "соответствующим уровнем доступа" in interaction.response.messages[0][0][0]
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.parametrize("role_id", [104, 105])
@pytest.mark.asyncio
async def test_privileged_levels_can_view_another_member(role_id: int) -> None:
    handler, sessions, _, _ = make_handler()
    interaction = make_interaction(user=make_member(20, role_ids=(role_id,)))

    await handler.handle(interaction, make_member(21))  # type: ignore[arg-type]

    assert sessions.calls == 1
    assert interaction.followup.messages


def test_multiple_roles_resolve_highest_and_authorize() -> None:
    role_ids = frozenset((101, 103, 104))

    assert resolve_kanami_member_role(role_ids, ROLES) is KanamiMemberRole.PURPLE
    assert can_view_member_statistics(
        viewer_user_id=20,
        target_user_id=21,
        viewer_role_ids=role_ids,
        role_configuration=ROLES,
    )


def test_missing_privileged_configuration_fails_closed_but_self_view_remains() -> None:
    configuration = MemberRoleConfiguration(guest_role_id=101)

    assert not can_view_member_statistics(
        viewer_user_id=20,
        target_user_id=21,
        viewer_role_ids=frozenset((104, 105)),
        role_configuration=configuration,
    )
    assert can_view_member_statistics(
        viewer_user_id=20,
        target_user_id=20,
        viewer_role_ids=frozenset(),
        role_configuration=configuration,
    )


@pytest.mark.asyncio
async def test_bot_target_and_dm_are_rejected_before_queries() -> None:
    handler, sessions, _, _ = make_handler()
    bot_interaction = make_interaction()
    dm_interaction = make_interaction(guild_id=None)

    await handler.handle(bot_interaction, make_member(21, bot=True))  # type: ignore[arg-type]
    await handler.handle(dm_interaction)  # type: ignore[arg-type]

    assert sessions.calls == 0
    assert "не ботов" in bot_interaction.response.messages[0][0][0]
    assert "настроенного сервера" in dm_interaction.response.messages[0][0][0]


@pytest.mark.asyncio
async def test_profile_service_handles_missing_optional_data() -> None:
    voice = RecordingVoiceRepository(all_time_seconds=0, last_30_days_seconds=0)
    achievements = RecordingAchievementRepository(count=0)
    service = MemberProfileService(
        voice,  # type: ignore[arg-type]
        achievements,
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
        min_session_seconds=10,
        role_configuration=MemberRoleConfiguration(),
    )

    profile = await service.get_profile(
        guild_id=10,
        subject=MemberProfileSubject(
            user_id=20,
            display_name="No data",
            joined_at=datetime(2026, 8, 23, 20, tzinfo=UTC),
        ),
        as_of=AS_OF,
    )

    assert profile.joined_on.isoformat() == "2026-08-24"  # type: ignore[union-attr]
    assert profile.server_age_days == 0
    assert profile.role is None
    assert profile.voice_all_time_seconds == 0
    assert profile.voice_last_30_days_seconds == 0
    assert profile.achievement_count == 0


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 сек"),
        (42, "42 сек"),
        (12 * 60, "12 мин"),
        (2 * 3600 + 5 * 60, "2 ч 05 мин"),
        (638 * 3600 + 24 * 60, "638 ч 24 мин"),
    ],
)
def test_profile_voice_duration_keeps_accumulated_hours(
    seconds: int, expected: str
) -> None:
    assert format_profile_voice_duration(seconds) == expected


def test_profile_voice_duration_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        format_profile_voice_duration(-1)
