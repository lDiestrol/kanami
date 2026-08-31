from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.discord import (
    DiscordStatsClient,
    MemberAnniversariesCommandHandler,
    build_member_anniversaries_embed,
)
from discord_stats_bot.features.member_anniversaries import (
    MemberAnniversary,
    MemberAnniversaryService,
    MemberJoinSnapshot,
)
from tests.support.discord import make_guild, make_interaction, make_member


def joined(
    user_id: int,
    year: int,
    month: int,
    day: int,
    *,
    name: str | None = None,
    bot: bool = False,
) -> MemberJoinSnapshot:
    return MemberJoinSnapshot(
        user_id=user_id,
        display_name=name or f"User {user_id}",
        joined_at=datetime(year, month, day, 12, tzinfo=UTC),
        is_bot=bot,
    )


def upcoming(
    members: tuple[MemberJoinSnapshot, ...],
    as_of: datetime,
) -> tuple[MemberAnniversary, ...]:
    return MemberAnniversaryService(ZoneInfo("UTC")).upcoming(
        members,
        as_of=as_of,
    )


def test_upcoming_anniversaries_are_sorted_by_days_until() -> None:
    result = upcoming(
        (
            joined(1, 2020, 8, 25, name="Later"),
            joined(2, 2022, 8, 21, name="Sooner"),
            joined(3, 2021, 8, 21, name="Also sooner"),
        ),
        datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert [(item.user_id, item.days_until) for item in result] == [
        (3, 1),
        (2, 1),
        (1, 5),
    ]


def test_anniversary_today_is_included_and_presented_as_today() -> None:
    result = upcoming(
        (joined(1, 2021, 8, 20, name="Ann_**"),),
        datetime(2026, 8, 20, 23, tzinfo=UTC),
    )

    assert result[0].days_until == 0
    assert result[0].years == 5
    embed = build_member_anniversaries_embed(result)
    assert "сегодня" in embed.description
    assert "5 лет" in embed.description
    assert "Ann\\_\\*\\*" in embed.description


def test_no_anniversaries_in_next_30_days_returns_empty_result() -> None:
    result = upcoming(
        (joined(1, 2020, 10, 1),),
        datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert result == ()
    embed = build_member_anniversaries_embed(result)
    assert embed.description == "В ближайшие 30 дней годовщин вступления нет."


def test_bots_and_members_without_join_date_are_excluded() -> None:
    result = upcoming(
        (
            joined(1, 2020, 8, 21, bot=True),
            MemberJoinSnapshot(2, "Unknown", None),
            joined(3, 2020, 8, 22),
        ),
        datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert [item.user_id for item in result] == [3]


def test_window_crosses_end_of_year() -> None:
    result = upcoming(
        (
            joined(1, 2020, 12, 31),
            joined(2, 2021, 1, 5),
            joined(3, 2020, 2, 1),
        ),
        datetime(2026, 12, 20, tzinfo=UTC),
    )

    assert [(item.user_id, item.days_until, item.years) for item in result] == [
        (1, 11, 6),
        (2, 16, 6),
    ]


def test_february_29_anniversary_uses_february_28_in_non_leap_year() -> None:
    result = upcoming(
        (joined(1, 2020, 2, 29),),
        datetime(2025, 2, 1, tzinfo=UTC),
    )

    assert result[0].anniversary_date.isoformat() == "2025-02-28"
    assert result[0].days_until == 27
    assert result[0].years == 5


def test_report_timezone_controls_calendar_today() -> None:
    service = MemberAnniversaryService(ZoneInfo("Asia/Yekaterinburg"))

    result = service.upcoming(
        (joined(1, 2020, 8, 21),),
        as_of=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
    )

    assert result[0].days_until == 0


def test_joining_today_is_not_a_zero_year_anniversary() -> None:
    result = upcoming(
        (joined(1, 2026, 8, 20),),
        datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert result == ()


def make_handler_client() -> DiscordStatsClient:
    handler = MemberAnniversariesCommandHandler(
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        clock=lambda: datetime(2026, 8, 20, tzinfo=UTC),
    )
    return DiscordStatsClient(
        guild_id=10,
        reference_provisioner=object(),  # type: ignore[arg-type]
        voice_reconciler=object(),  # type: ignore[arg-type]
        voice_event_handler=object(),  # type: ignore[arg-type]
        member_anniversaries_command_handler=handler,
    )


def test_anniversaries_is_registered_for_configured_guild_only() -> None:
    client = make_handler_client()

    assert client.tree.get_command("anniversaries", guild=client._command_guild)
    assert client.tree.get_command("anniversaries") is None


@pytest.mark.asyncio
async def test_handler_reads_cached_members_and_responds_publicly() -> None:
    handler = MemberAnniversariesCommandHandler(
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        clock=lambda: datetime(2026, 8, 20, tzinfo=UTC),
    )
    guild = make_guild(
        members=(
            make_member(
                1,
                display_name="Member",
                joined_at=datetime(2020, 8, 21, tzinfo=UTC),
            ),
            make_member(
                2,
                bot=True,
                joined_at=datetime(2020, 8, 21, tzinfo=UTC),
            ),
        )
    )
    interaction = make_interaction(guild=guild)

    await handler.handle(interaction)  # type: ignore[arg-type]

    _, response = interaction.response.messages[0]
    assert response["ephemeral"] is False
    assert "Member" in response["embed"].description
    assert "User 2" not in response["embed"].description
    assert response["allowed_mentions"].users is False


@pytest.mark.asyncio
async def test_anniversaries_handler_rejects_dm() -> None:
    handler = MemberAnniversariesCommandHandler(
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
    )
    interaction = make_interaction(guild_id=None)

    await handler.handle(interaction)  # type: ignore[arg-type]

    assert interaction.response.messages[0][1]["ephemeral"] is True
