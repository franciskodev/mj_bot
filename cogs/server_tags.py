import asyncio
import logging
from typing import TYPE_CHECKING, Any

import disnake
from disnake.ext import commands, tasks

from config import GUILD_ID, TARGET_ROLE_IDS
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import IntoleranceBot


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

    @tasks.loop(hours=1)
    async def check_server_tags(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        role_ids = await self.family_role_ids()
        active_user_ids = {
            profile["discord_id"]
            for profile in await self.bot.database.get_active_users()
        }
        members = {
            member.id: member
            for member in guild.members
            if member.id in active_user_ids
            and not member.bot
            and (
                member.guild_permissions.administrator
                or any(role.id in role_ids for role in member.roles)
            )
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
                continue

            compliant = bool(
                primary_guild
                and str(primary_guild.get("identity_guild_id")) == str(GUILD_ID)
                and primary_guild.get("identity_enabled") is True
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
                        f"Привет, {member.mention}! У вас не отображается тег сервера "
                        f"**{guild.name}**. Пожалуйста, выберите тег нашего сервера в "
                        "настройках профиля Discord и включите его отображение."
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

            await self.bot.database.record_server_tag_check(
                user_id,
                is_compliant=compliant,
                reminded=should_remind,
                delivered=delivered,
            )
            await asyncio.sleep(0.25)

        if reminded:
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
