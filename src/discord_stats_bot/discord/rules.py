"""Discord presentation and persistent acceptance control for Rules v1."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.rules import (
    NoPublishedRulesetError,
    RulesAcceptanceStatistics,
    RulesetRecord,
    RulesRepository,
    RulesService,
)
from discord_stats_bot.persistence.repositories import SqlAlchemyRulesRepository

logger = logging.getLogger(__name__)

RULES_ACCEPT_BUTTON_CUSTOM_ID = "kanami:rules:accept:v1"
RULES_ACCEPT_ROLE_REASON = "Принятие текущей версии правил сервера"
RulesRepositoryFactory = Callable[[AsyncSession], RulesRepository]


class RulesAcceptedRoleGrantStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    ALREADY_PRESENT = "already_present"
    GRANTED = "granted"
    ROLE_MISSING = "role_missing"
    GRANT_FAILED = "grant_failed"

    @property
    def failed(self) -> bool:
        return self in {self.ROLE_MISSING, self.GRANT_FAILED}


def build_rules_embed(ruleset: RulesetRecord) -> discord.Embed:
    """Render one current ruleset without embedding policy in the handler."""

    embed = discord.Embed(
        title=ruleset.title,
        description=ruleset.content,
        colour=0xEB459E,
    )
    embed.set_footer(text=f"Версия {ruleset.version}")
    return embed


def build_rules_status_embed(
    statistics: RulesAcceptanceStatistics,
) -> discord.Embed:
    ruleset = statistics.ruleset
    published = (
        discord.utils.format_dt(ruleset.published_at, style="F")
        if ruleset.published_at is not None
        else "н/д"
    )
    embed = discord.Embed(
        title="Rules v1 — состояние",
        colour=0x57F287,
    )
    embed.add_field(name="Текущая версия", value=ruleset.version, inline=False)
    embed.add_field(
        name="Публикация",
        value=f"Статус: {ruleset.status.value}\nДата: {published}",
        inline=False,
    )
    embed.add_field(
        name="Приняли",
        value=str(statistics.accepted_count),
        inline=False,
    )
    return embed


class RulesAcceptanceView(discord.ui.View):
    """Persistent view whose stable custom ID survives process restarts."""

    def __init__(self, handler: "RulesCommandHandler") -> None:
        super().__init__(timeout=None)
        self._handler = handler

    @discord.ui.button(
        label="✅ Принимаю правила",
        style=discord.ButtonStyle.success,
        custom_id=RULES_ACCEPT_BUTTON_CUSTOM_ID,
    )
    async def accept_rules(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button["RulesAcceptanceView"],
    ) -> None:
        del button
        await self._handler.accept_current(interaction)


class RulesCommandHandler:
    """Own Discord context checks and transaction boundaries for Rules v1."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        accepted_role_id: int | None = None,
        repository_factory: RulesRepositoryFactory = SqlAlchemyRulesRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._accepted_role_id = accepted_role_id
        self._repository_factory = repository_factory
        self._clock = clock

    def create_persistent_view(self) -> RulesAcceptanceView:
        return RulesAcceptanceView(self)

    async def show(self, interaction: discord.Interaction) -> None:
        if not await self._valid_member(interaction):
            return
        async with self._session_factory() as session:
            ruleset = await RulesService(
                self._repository_factory(session)
            ).get_current_published(self._guild_id)
        if ruleset is None:
            await interaction.response.send_message(
                "На сервере пока нет опубликованных правил.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=build_rules_embed(ruleset),
            view=self.create_persistent_view(),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def accept_current(self, interaction: discord.Interaction) -> None:
        member = await self._valid_member(interaction)
        if member is None:
            return
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    result = await RulesService(
                        self._repository_factory(session)
                    ).accept_current(
                        self._guild_id,
                        member.id,
                        accepted_at=self._clock(),
                    )
        except NoPublishedRulesetError:
            await interaction.response.send_message(
                "На сервере пока нет опубликованных правил.", ephemeral=True
            )
            return

        role_status = await self._grant_accepted_role(member)
        if role_status.failed:
            message = (
                f"Правила версии {result.ruleset.version} успешно приняты, но Kanami "
                "не смогла выдать роль доступа. Обратись к администрации."
            )
        elif result.newly_accepted:
            message = (
                f"❤️ Правила версии {result.ruleset.version} приняты. Добро пожаловать!"
            )
        else:
            message = f"Ты уже принял правила версии {result.ruleset.version}."
        await interaction.response.send_message(message, ephemeral=True)

    async def show_status(self, interaction: discord.Interaction) -> None:
        member = await self._valid_member(interaction, require_manager=True)
        if member is None:
            return
        async with self._session_factory() as session:
            statistics = await RulesService(
                self._repository_factory(session)
            ).get_current_statistics(self._guild_id)
        if statistics is None:
            await interaction.response.send_message(
                "На сервере нет текущей опубликованной версии правил.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=build_rules_status_embed(statistics),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _valid_member(
        self,
        interaction: discord.Interaction,
        *,
        require_manager: bool = False,
    ) -> discord.Member | None:
        guild = interaction.guild
        member = interaction.user
        allowed = (
            guild is not None
            and guild.id == self._guild_id
            and isinstance(member, discord.Member)
            and not member.bot
        )
        if allowed and require_manager:
            allowed = member.guild_permissions.manage_guild
        if allowed:
            return member
        message = (
            "Эта команда доступна только участникам с правом «Управлять сервером»."
            if require_manager
            else "Эта команда доступна только участникам настроенного сервера."
        )
        await interaction.response.send_message(message, ephemeral=True)
        return None

    async def _grant_accepted_role(
        self, member: discord.Member
    ) -> RulesAcceptedRoleGrantStatus:
        if self._accepted_role_id is None:
            return RulesAcceptedRoleGrantStatus.NOT_CONFIGURED
        role = member.guild.get_role(self._accepted_role_id)
        if role is None:
            logger.warning(
                "Rules accepted role is unavailable guild_id=%s role_id=%s user_id=%s",
                self._guild_id,
                self._accepted_role_id,
                member.id,
            )
            return RulesAcceptedRoleGrantStatus.ROLE_MISSING
        if role in member.roles:
            return RulesAcceptedRoleGrantStatus.ALREADY_PRESENT
        try:
            await member.add_roles(role, reason=RULES_ACCEPT_ROLE_REASON)
        except Exception as exc:
            logger.warning(
                "Rules accepted role grant failed guild_id=%s role_id=%s user_id=%s "
                "error_type=%s",
                self._guild_id,
                self._accepted_role_id,
                member.id,
                type(exc).__name__,
            )
            return RulesAcceptedRoleGrantStatus.GRANT_FAILED
        return RulesAcceptedRoleGrantStatus.GRANTED
