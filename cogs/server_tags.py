import asyncio
import logging
from typing import TYPE_CHECKING, Any

import disnake
from disnake.ext import commands, tasks

from config import GUILD_ID, SERVER_TAG_LOG_CHANNEL_ID, TARGET_ROLE_IDS
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import IntoleranceBot


REQUIRED_SERVER_TAG = "INRP"


class ServerTagsCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot
        self.check_server_tags.start()

    def cog_unload(self) -> None:
        self.check_server_tags.cancel()

    async def family_role_ids(self) -> set[int]:
        role_ids = set(TARGET_ROLE_IDS)
        config = await self.bot.database.get_application_config(GUILD_ID)
        if config is not None:
            role_ids.update(
                {config["interpolov_role_id"], config["intolerance_role_id"]}
            )
        return role_ids

    async def fetch_primary_guild(self, user_id: int) -> dict[str, Any] | None:
        user_data = await self.bot.http.get_user(user_id)
        primary_guild = user_data.get("primary_guild")
        return primary_guild if isinstance(primary_guild, dict) else None

    async def send_tag_log(self, embed: disnake.Embed) -> None:
        channel = self.bot.get_channel(SERVER_TAG_LOG_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(SERVER_TAG_LOG_CHANNEL_ID)
            except disnake.DiscordException:
                logging.exception("Could not fetch server tag log channel")
                return
        if not isinstance(channel, (disnake.TextChannel, disnake.Thread)):
            logging.error(
                "Server tag log channel %s is not a text channel",
                SERVER_TAG_LOG_CHANNEL_ID,
            )
            return
        try:
            await channel.send(embed=embed)
        except disnake.DiscordException:
            logging.exception("Could not send server tag log")

    async def log_tag_check_error(self, member: disnake.Member) -> None:
        embed = disnake.Embed(
            title="❌ Ошибка проверки серверного тега",
            description=f"Участник: {member.mention} (`{member.id}`)",
            color=disnake.Color.red(),
            timestamp=disnake.utils.utcnow(),
        )
        embed.add_field(
            name="Результат",
            value="Discord API не вернул данные о теге. Уведомление не отправлялось.",
            inline=False,
        )
        embed.set_footer(text="INTERPOLOV & INTOLERANCE • Журнал серверных тегов")
        await self.send_tag_log(embed)

    async def log_tag_notification(
        self,
        member: disnake.Member,
        primary_guild: dict[str, Any] | None,
        *,
        delivered: bool,
        family_role_ids: set[int],
    ) -> None:
        identity_guild_id = primary_guild.get("identity_guild_id") if primary_guild else None
        identity_enabled = primary_guild.get("identity_enabled") if primary_guild else None
        identity_tag = primary_guild.get("tag") if primary_guild else None
        member_family_roles = [
            role.mention for role in member.roles if role.id in family_role_ids
        ]

        if primary_guild is None:
            reason = "Основной сервер или серверный тег не выбран."
        elif identity_enabled is not True:
            reason = f"Отображение тега {REQUIRED_SERVER_TAG} выключено."
        elif str(identity_tag).strip().upper() != REQUIRED_SERVER_TAG:
            reason = f"Выбран тег `{identity_tag}`, а требуется `{REQUIRED_SERVER_TAG}`."
        else:
            reason = "Тег не соответствует требованиям."

        embed = disnake.Embed(
            title=(
                "✅ Уведомление о теге доставлено"
                if delivered
                else "❌ Уведомление о теге не доставлено"
            ),
            description=(
                f"Участник: {member.mention} (`{member.id}`)\n"
                f"Пользователь: **{member}**"
            ),
            color=disnake.Color.green() if delivered else disnake.Color.red(),
            timestamp=disnake.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Причина уведомления", value=reason, inline=False)
        embed.add_field(
            name="Семейные роли",
            value=", ".join(member_family_roles) or "Права администратора",
            inline=False,
        )
        embed.add_field(
            name="Выбранный сервер тега",
            value=f"`{identity_guild_id}`" if identity_guild_id else "Не выбран",
            inline=True,
        )
        embed.add_field(
            name="Отображение включено",
            value="Да" if identity_enabled is True else "Нет",
            inline=True,
        )
        embed.add_field(
            name="Тег",
            value=f"`{identity_tag}`" if identity_tag else "Не указан",
            inline=True,
        )
        embed.add_field(
            name="Доставка в ЛС",
            value="Успешно" if delivered else "Не удалось доставить",
            inline=False,
        )
        embed.set_footer(text="INTERPOLOV & INTOLERANCE • Журнал серверных тегов")
        await self.send_tag_log(embed)

    @tasks.loop(hours=1)
    async def check_server_tags(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        role_ids = await self.family_role_ids()
        members = {
            member.id: member
            for member in guild.members
            if not member.bot
            and any(role.id in role_ids for role in member.roles)
        }
        due_ids = await self.bot.database.get_due_server_tag_check_user_ids(
            list(members)
        )
        reminded = 0
        failed = 0
        for user_id in due_ids:
            member = members.get(user_id)
            if member is None:
                continue
            try:
                primary_guild = await self.fetch_primary_guild(user_id)
            except disnake.DiscordException:
                logging.exception("Could not check server tag for member %s", user_id)
                await self.log_tag_check_error(member)
                continue

            compliant = bool(
                primary_guild
                and primary_guild.get("identity_enabled") is True
                and str(primary_guild.get("tag", "")).strip().upper()
                == REQUIRED_SERVER_TAG
            )
            should_remind = (
                not compliant
                and await self.bot.database.is_server_tag_reminder_due(user_id)
            )
            delivered: bool | None = None
            if should_remind:
                embed = disnake.Embed(
                    title="🏷️ Включите тег нашего сервера",
                    description=(
                        f"Привет, {member.mention}! У вас не отображается серверный "
                        f"тег **{REQUIRED_SERVER_TAG}**. Пожалуйста, выберите его в "
                        "настройках профиля Discord и включите отображение."
                    ),
                    color=disnake.Color.gold(),
                    timestamp=disnake.utils.utcnow(),
                )
                embed.set_footer(
                    text="INTERPOLOV & INTOLERANCE • Напоминание о теге"
                )
                delivered = True
                try:
                    await member.send(embed=embed)
                except disnake.DiscordException:
                    delivered = False
                    failed += 1
                    logging.warning("Could not send server tag reminder to %s", user_id)
                reminded += 1

                await self.log_tag_notification(
                    member,
                    primary_guild,
                    delivered=delivered,
                    family_role_ids=role_ids,
                )

            await self.bot.database.record_server_tag_check(
                user_id,
                is_compliant=compliant,
                reminded=should_remind,
                delivered=delivered,
            )
            await asyncio.sleep(0.25)

        if reminded:
            summary_embed = disnake.Embed(
                title="🏷️ Итоги проверки серверных тегов",
                description=(
                    f"Участников в очереди проверки: **{len(due_ids)}**\n"
                    f"Попыток уведомления: **{reminded}**\n"
                    f"Доставлено: **{reminded - failed}**\n"
                    f"Не доставлено: **{failed}**"
                ),
                color=disnake.Color.gold(),
                timestamp=disnake.utils.utcnow(),
            )
            summary_embed.set_footer(
                text="INTERPOLOV & INTOLERANCE • Журнал серверных тегов"
            )
            await self.send_tag_log(summary_embed)
            await send_admin_log(
                self.bot,
                title="🏷️ Проверка тегов сервера",
                description=(
                    f"Проверено участников: **{len(due_ids)}**\n"
                    f"Отправлено напоминаний: **{reminded - failed}**\n"
                    f"Не доставлено: **{failed}**"
                ),
                color=disnake.Color.gold(),
            )

    @check_server_tags.before_loop
    async def before_server_tag_check(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)
