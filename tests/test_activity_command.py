from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.discord import (
    VoiceActivityCommandHandler,
    build_voice_activity_embed,
)
from discord_stats_bot.features.voice_statistics import (
    VoiceActivityInterval,
    VoiceActivityPeriod,
    aggregate_voice_activity,
)
from tests.support.discord import make_interaction
from tests.support.persistence import FakeSessionFactory

AS_OF = datetime(2026, 1, 12, tzinfo=UTC)


def report(*intervals: VoiceActivityInterval):
    return aggregate_voice_activity(
        intervals,
        period=VoiceActivityPeriod.LAST_30_DAYS,
        as_of=AS_OF,
        report_timezone=ZoneInfo("Europe/Moscow"),
    )


def test_activity_embed_shows_top_weekday_quiet_period_and_timezone() -> None:
    embed = build_voice_activity_embed(
        report(
            VoiceActivityInterval(
                AS_OF - timedelta(days=1, hours=3),
                AS_OF - timedelta(days=1, hours=2),
                quality="estimated",
            )
        )
    )

    assert embed.title == "🕘 Когда сервер оживает"
    assert "последние 30 дней" in embed.description
    assert [field.name for field in embed.fields] == [
        "Самое активное время сервера",
        "Самый активный день",
        "Самое тихое время",
        "Активность",
        "Примечание",
    ]
    assert "🥇" in embed.fields[0].value
    assert embed.fields[1].value in {
        "понедельник",
        "вторник",
        "среда",
        "четверг",
        "пятница",
        "суббота",
        "воскресенье",
    }
    assert " — " in embed.fields[2].value
    heatmap = embed.fields[3].value
    assert heatmap.startswith("```text\n")
    assert heatmap.endswith("\n```")
    assert heatmap.splitlines()[1:] == [
        "      Пн Вт Ср Чт Пт Сб Вс",
        "00:00 · · · · · · █",
        "03:00 · · · · · · ·",
        "06:00 · · · · · · ·",
        "09:00 · · · · · · ·",
        "12:00 · · · · · · ·",
        "15:00 · · · · · · ·",
        "18:00 · · · · · · ·",
        "21:00 · · · · · · ·",
        "```",
    ]
    assert embed.footer.text == "Часовой пояс: Europe/Moscow"


def test_activity_embed_handles_empty_data_without_zero_top_three() -> None:
    embed = build_voice_activity_embed(report())

    assert len(embed.fields) == 1
    assert embed.fields[0].name == "Недостаточно данных"
    assert "недостаточно голосовой активности" in embed.fields[0].value
    assert "🥇" not in embed.fields[0].value


class RecordingRepository:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[int, datetime, object]] = []

    async def get_activity_intervals(
        self, guild_id: int, started_at: datetime, query: object
    ) -> tuple[VoiceActivityInterval, ...]:
        self.calls.append((guild_id, started_at, query))
        if self.error is not None:
            raise self.error
        return (
            VoiceActivityInterval(
                AS_OF - timedelta(hours=2), AS_OF - timedelta(hours=1)
            ),
        )


def make_handler(
    repository: RecordingRepository,
) -> tuple[VoiceActivityCommandHandler, FakeSessionFactory]:
    session_factory = FakeSessionFactory()
    return (
        VoiceActivityCommandHandler(
            session_factory,  # type: ignore[arg-type]
            guild_id=10,
            report_timezone=ZoneInfo("UTC"),
            min_session_seconds=10,
            repository_factory=lambda session: repository,  # type: ignore[arg-type]
            clock=lambda: AS_OF,
        ),
        session_factory,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, VoiceActivityPeriod.LAST_30_DAYS),
        ("7d", VoiceActivityPeriod.LAST_7_DAYS),
        ("30d", VoiceActivityPeriod.LAST_30_DAYS),
        ("90d", VoiceActivityPeriod.LAST_90_DAYS),
    ],
)
@pytest.mark.asyncio
async def test_activity_periods_are_public_and_query_only_the_selected_window(
    value: str | None,
    expected: VoiceActivityPeriod,
) -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)
    interaction = make_interaction()

    await handler.handle(interaction, value)  # type: ignore[arg-type]

    assert repository.calls[0][0] == 10
    assert repository.calls[0][1] == AS_OF - timedelta(days=expected.days)
    assert interaction.response.deferred == [{"ephemeral": False, "thinking": True}]
    kwargs = interaction.followup.messages[0][1]
    assert kwargs["ephemeral"] is False
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs["allowed_mentions"].users is False
    assert kwargs["allowed_mentions"].roles is False
    assert session_factory.sessions[0].closed is True


@pytest.mark.parametrize(("guild_id", "bot"), [(11, False), (None, False), (10, True)])
@pytest.mark.asyncio
async def test_activity_validation_rejects_without_database(
    guild_id: int | None,
    bot: bool,
) -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)
    interaction = make_interaction(guild_id=guild_id, bot=bot)

    await handler.handle(interaction)  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_activity_invalid_period_and_query_failure_are_safe() -> None:
    repository = RecordingRepository(error=RuntimeError("offline"))
    handler, session_factory = make_handler(repository)

    invalid = make_interaction()
    await handler.handle(invalid, "all")  # type: ignore[arg-type]
    assert invalid.response.messages[0][1]["ephemeral"] is True
    assert session_factory.sessions == []

    failed = make_interaction()
    await handler.handle(failed, "7d")  # type: ignore[arg-type]
    args, kwargs = failed.followup.messages[0]
    assert args == ("Не удалось получить активность сервера. Попробуйте позже.",)
    assert kwargs == {"ephemeral": False}
