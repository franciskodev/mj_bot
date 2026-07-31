import logging
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from config import DEFAULT_MEMBER_ROLE_ID, GUILD_ID, TARGET_ROLE_IDS
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import InterpolBot


class CoreCog(commands.Cog):
    def __init__(self, bot: "InterpolBot") -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        logging.info(
            "Bot is ready: %s (%s)",
            self.bot.user,
            self.bot.user.id if self.bot.user else "unknown",
        )

        for guild in self.bot.guilds:
            if guild.id != GUILD_ID:
                logging.warning("Leaving unauthorized guild: %s (%s)", guild.name, guild.id)
                await guild.leave()



        guild = self.bot.get_guild(GUILD_ID)
        if guild is not None:
            for profile in await self.bot.database.get_active_users():
                member = guild.get_member(profile["discord_id"])
                has_family_role = member is not None and any(
                    role.id in TARGET_ROLE_IDS for role in member.roles
                )
                if not has_family_role:
                    await self.bot.database.deactivate_user(
                        profile["discord_id"], "Отсутствует семейная роль"
                    )

    @commands.Cog.listener()
    async def on_member_join(self, member: disnake.Member) -> None:
        if member.guild.id != GUILD_ID or member.bot:
            return

        role = member.guild.get_role(DEFAULT_MEMBER_ROLE_ID)
        if role is None:
            logging.error("Default member role %s was not found", DEFAULT_MEMBER_ROLE_ID)
            await send_admin_log(
                self.bot,
                title="⚠️ Стартовая роль не выдана",
                description=(
                    f"Пользователь: {member.mention} (`{member.id}`)\n"
                    f"Роль `{DEFAULT_MEMBER_ROLE_ID}` не найдена."
                ),
                color=disnake.Color.orange(),
            )
            return

        try:
            await member.add_roles(role, reason="Автоматическая роль при входе на сервер")
        except disnake.DiscordException:
            logging.exception("Could not add default role to member %s", member.id)
            await send_admin_log(
                self.bot,
                title="⚠️ Ошибка выдачи стартовой роли",
                description=(
                    f"Пользователь: {member.mention} (`{member.id}`)\n"
                    f"Роль: {role.mention} (`{role.id}`)"
                ),
                color=disnake.Color.orange(),
            )
            return

        await send_admin_log(
            self.bot,
            title="👋 Новый участник",
            description=(
                f"Пользователь: {member.mention} (`{member.id}`)\n"
                f"Автоматически выдана роль {role.mention}."
            ),
            color=disnake.Color.green(),
        )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: disnake.Guild) -> None:
        if guild.id != GUILD_ID:
            logging.warning("Leaving unauthorized guild: %s (%s)", guild.name, guild.id)
            await guild.leave()
