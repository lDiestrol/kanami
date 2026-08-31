"""Discord presentation adapter for the guild-only ``/anniversaries`` command."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.discord.server_settings import GuildServerSettingsProvider
from discord_stats_bot.features.member_anniversaries import (
    MemberAnniversary,
    MemberAnniversaryNotificationRepository,
    MemberAnniversaryNotificationService,
    MemberAnniversaryService,
    MemberJoinSnapshot,
)
from discord_stats_bot.persistence.repositories.member_anniversaries import (
    SqlAlchemyMemberAnniversaryRepository,
)

ANNIVERSARY_WINDOW_DAYS = 30
ANNIVERSARY_DAILY_CHECK_TIME = time(hour=0, minute=5)

logger = logging.getLogger(__name__)

MemberAnniversaryRepositoryFactory = Callable[
    [AsyncSession], MemberAnniversaryNotificationRepository
]


def _plural(value: int, forms: tuple[str, str, str]) -> str:
    if value % 10 == 1 and value % 100 != 11:
        return forms[0]
    if value % 10 in (2, 3, 4) and value % 100 not in (12, 13, 14):
        return forms[1]
    return forms[2]


def _format_anniversary(entry: MemberAnniversary) -> str:
    name = discord.utils.escape_markdown(entry.display_name)
    years = f"{entry.years} {_plural(entry.years, ('год', 'года', 'лет'))}"
    if entry.days_until == 0:
        timing = "сегодня"
    else:
        day_word = _plural(entry.days_until, ("день", "дня", "дней"))
        timing = f"через {entry.days_until} {day_word}"
    return f"• **{name}** — исполнится **{years}** на сервере, {timing}"


def build_member_anniversaries_embed(
    anniversaries: tuple[MemberAnniversary, ...],
) -> discord.Embed:
    """Render the upcoming anniversary list in the common Kanami style."""

    embed = discord.Embed(title="Ближайшие годовщины", colour=0x7C5CFC)
    if anniversaries:
        embed.description = "\n".join(
            _format_anniversary(entry) for entry in anniversaries
        )
    else:
        embed.description = "В ближайшие 30 дней годовщин вступления нет."
    embed.set_footer(text="Учитывается дата вступления по времени сервера Kanami.")
    return embed


def build_member_anniversary_notification_embed(
    *,
    user_id: int,
    years: int,
) -> discord.Embed:
    """Render one automatic anniversary congratulations message."""

    year_word = _plural(years, ("год", "года", "лет"))
    return discord.Embed(
        title="Годовщина на сервере!",
        description=(
            f"🎉 Сегодня <@{user_id}> уже **{years} {year_word}** на сервере!"
        ),
        colour=0x7C5CFC,
    )


class MemberAnniversariesCommandHandler:
    """Adapt cached Discord members to the anniversary calculation service."""

    def __init__(
        self,
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._guild_id = guild_id
        self._service = MemberAnniversaryService(report_timezone)
        self._clock = clock

    async def handle(self, interaction: discord.Interaction) -> None:
        """Return upcoming cached non-bot member anniversaries publicly."""

        if (
            interaction.guild_id != self._guild_id
            or interaction.guild is None
            or interaction.user.bot
        ):
            await interaction.response.send_message(
                "Эта команда доступна только участникам настроенного сервера.",
                ephemeral=True,
            )
            return

        members = tuple(
            MemberJoinSnapshot(
                user_id=member.id,
                display_name=member.display_name,
                joined_at=member.joined_at,
                is_bot=member.bot,
            )
            for member in interaction.guild.members
        )
        anniversaries = self._service.upcoming(
            members,
            as_of=self._clock(),
            days=ANNIVERSARY_WINDOW_DAYS,
        )
        await interaction.response.send_message(
            embed=build_member_anniversaries_embed(anniversaries),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class MemberAnniversaryCheckRunner:
    """Run an immediate check and then at a fixed local wall-clock time daily."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        wake_delivery: Callable[[], None] | None = None,
        repository_factory: MemberAnniversaryRepositoryFactory = (
            SqlAlchemyMemberAnniversaryRepository
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        check_time: time = ANNIVERSARY_DAILY_CHECK_TIME,
        settings_provider: GuildServerSettingsProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._report_timezone = report_timezone
        self._wake_delivery = wake_delivery
        self._repository_factory = repository_factory
        self._clock = clock
        self._check_time = check_time
        self._settings_provider = settings_provider
        self._task: asyncio.Task[None] | None = None
        self._client: discord.Client | None = None

    def start(self, client: discord.Client) -> None:
        """Start one worker; repeated READY/RESUME calls are harmless."""

        if self._task is not None and not self._task.done():
            return
        self._client = client
        self._task = asyncio.create_task(
            self._run(),
            name="member-anniversary-check-loop",
        )

    async def stop(self) -> None:
        """Cancel and await the worker during client shutdown."""

        task = self._task
        self._task = None
        self._client = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def run_once(self, client: discord.Client) -> int:
        """Enqueue today's cached member anniversaries in one transaction."""

        if (
            self._settings_provider is not None
            and (await self._settings_provider.get()).anniversary_channel_id is None
        ):
            return 0
        guild = client.get_guild(self._guild_id)
        if guild is None:
            raise RuntimeError("configured guild is unavailable")
        members = tuple(
            MemberJoinSnapshot(
                user_id=member.id,
                display_name=member.display_name,
                joined_at=member.joined_at,
                is_bot=member.bot,
            )
            for member in guild.members
        )
        as_of = self._clock()
        async with self._session_factory.begin() as session:
            service = MemberAnniversaryNotificationService(
                self._repository_factory(session),
                report_timezone=self._report_timezone,
            )
            enqueued = await service.enqueue_today(
                guild_id=self._guild_id,
                members=members,
                as_of=as_of,
            )
        if enqueued and self._wake_delivery is not None:
            self._wake_delivery()
        if enqueued:
            logger.info(
                "Member anniversaries enqueued guild_id=%s count=%s",
                self._guild_id,
                enqueued,
            )
        return enqueued

    def _seconds_until_next_check(self) -> float:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        local_now = now.astimezone(self._report_timezone)
        target_date = local_now.date()
        if local_now.timetz().replace(tzinfo=None) >= self._check_time:
            target_date += timedelta(days=1)
        target = datetime.combine(
            target_date,
            self._check_time,
            tzinfo=self._report_timezone,
        )
        return max(0.0, (target.astimezone(UTC) - now.astimezone(UTC)).total_seconds())

    async def _run(self) -> None:
        while True:
            client = self._client
            if client is None:
                return
            try:
                await self.run_once(client)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Member anniversary check failed guild_id=%s",
                    self._guild_id,
                )
            await asyncio.sleep(self._seconds_until_next_check())
