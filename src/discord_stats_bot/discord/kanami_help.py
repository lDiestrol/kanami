"""Static Discord help presentation for Kanami commands."""

import discord


def build_kanami_help_embed() -> discord.Embed:
    """Build the database-independent command reference embed."""

    embed = discord.Embed(
        title="Kanami — статистика сервера",
        description=(
            "Kanami собирает и показывает статистику голосовой и текстовой "
            "активности участников сервера."
        ),
        colour=0x7C5CFC,
    )
    embed.add_field(
        name="Статистика",
        value=(
            "`/profile` — паспорт участника с Voice, ролью и достижениями.\n"
            "`/stats` — профиль своей или выбранной голосовой активности "
            "за период.\n"
            "`/top` — TOP-10 участников по времени в голосовых каналах.\n"
            "`/topmessages` — TOP-10 участников по количеству сообщений.\n"
            "`/games` — игровая активность своя или выбранного участника.\n"
            "`/channels` — рейтинг голосовых каналов.\n"
            "`/channelstats` — подробная статистика выбранного голосового "
            "или Stage-канала.\n"
            "`/together` — показать совместную голосовую статистику двух участников."
            "\n`/serverstats` — общая голосовая статистика сервера.\n"
            "`/activity` — когда сервер наиболее активен.\n"
            "`/achievements` — достижения свои или выбранного участника.\n"
            "`/anniversaries` — ближайшие годовщины вступления участников."
            "\n`/rules` — текущая версия правил и кнопка принятия."
        ),
        inline=False,
    )
    embed.add_field(
        name="Помощь",
        value="`/help` — показать эту справку.",
        inline=False,
    )
    embed.add_field(
        name="Администрирование",
        value=(
            "`/health` — приватная read-only диагностика Kanami "
            "для участников с правом «Управлять сервером».\n"
            "`/rules-status` — версия правил и число принявших для администраторов."
        ),
        inline=False,
    )
    embed.set_footer(
        text="Введите / — Discord покажет доступные команды и их параметры."
    )
    return embed
