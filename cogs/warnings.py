import logging
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from config import GUILD_IDS
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import IntoleranceBot


async def notify_warning(
    bot: "IntoleranceBot",
    member: disnake.Member,
    administrator: disnake.Member | disnake.User,
    reason: str,
    count: int,
) -> bool:
    embed = disnake.Embed(
        title="⚠️ Вам выдан выговор",
        description=(
            f"Сервер: **{member.guild.name}**\n"
            f"Причина: **{reason}**\n"
            f"Активных выговоров: **{count}/3**"
        ),
        color=disnake.Color.orange(),
        timestamp=disnake.utils.utcnow(),
    )
    delivered = True
    try:
        await member.send(embed=embed)
    except disnake.DiscordException:
        delivered = False
        logging.warning("Could not send disciplinary warning DM to %s", member.id)
    await send_admin_log(
        bot,
        title="⚠️ Выдан выговор",
        description=(
            f"Администратор: {administrator.mention}\n"
            f"Пользователь: {member.mention} (`{member.id}`)\n"
            f"Причина: **{reason}**\n"
            f"Активных выговоров: **{count}/3**\n"
            f"Личное сообщение: **{'доставлено' if delivered else 'не доставлено'}**"
        ),
        color=disnake.Color.orange(),
    )
    return delivered


class WarningsCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot

    @commands.slash_command(
        name="warning",
        description="Управление выговорами",
        guild_ids=GUILD_IDS,
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    async def warning(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @warning.sub_command(name="issue", description="Выдать выговор участнику")
    @commands.has_permissions(administrator=True)
    async def warning_issue(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Участник"),
        reason: str = commands.Param(description="Причина", max_length=500),
    ) -> None:
        await inter.response.defer(ephemeral=True)
        profile = await self.bot.database.get_user(member.id)
        if profile is None:
            await inter.edit_original_response(
                content="Пользователь отсутствует в базе семьи."
            )
            return
        row, count, error = await self.bot.database.issue_disciplinary_warning(
            member.id, inter.author.id, reason
        )
        if error == "limit_reached":
            await inter.edit_original_response(
                content=f"У {member.mention} уже максимальное количество выговоров (**3/3**)."
            )
            return
        await notify_warning(self.bot, member, inter.author, reason, count)
        await inter.edit_original_response(
            content=f"⚠️ {member.mention} выдан выговор. Активных: **{count}/3**."
        )

    @warning.sub_command(name="list", description="Посмотреть историю выговоров участника")
    @commands.has_permissions(administrator=True)
    async def warning_list(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Участник"),
    ) -> None:
        rows = await self.bot.database.get_disciplinary_warnings(member.id)
        active = sum(row["revoked_at"] is None for row in rows)
        lines = []
        for row in rows[:20]:
            status = "активен" if row["revoked_at"] is None else "снят"
            lines.append(
                f"`#{row['id']}` • **{status}** • <t:{int(row['created_at'].timestamp())}:d>\n"
                f"> {row['reason']}"
            )
        embed = disnake.Embed(
            title=f"Выговоры — {member.display_name}",
            description="\n".join(lines) if lines else "Выговоров нет.",
            color=disnake.Color.orange() if active else disnake.Color.green(),
        )
        embed.set_footer(text=f"Активных: {active}/3 • Показано последних: {min(len(rows), 20)}")
        await inter.response.send_message(embed=embed, ephemeral=True)

    @warning.sub_command(name="revoke", description="Снять активный выговор")
    @commands.has_permissions(administrator=True)
    async def warning_revoke(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Участник"),
        warning_id: int = commands.Param(description="Номер выговора", min_value=1),
    ) -> None:
        row, count = await self.bot.database.revoke_disciplinary_warning(
            warning_id, member.id, inter.author.id
        )
        if row is None:
            await inter.response.send_message(
                "Активный выговор с таким номером у пользователя не найден.", ephemeral=True
            )
            return
        await inter.response.send_message(
            f"✅ Выговор `#{warning_id}` у {member.mention} снят. Активных: **{count}/3**.",
            ephemeral=True,
        )
        await send_admin_log(
            self.bot,
            title="✅ Выговор снят",
            description=(
                f"Администратор: {inter.author.mention}\n"
                f"Пользователь: {member.mention} (`{member.id}`)\n"
                f"Выговор: `#{warning_id}`\nАктивных осталось: **{count}/3**"
            ),
            color=disnake.Color.green(),
        )
