import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands, tasks

from config import GUILD_ID, TARGET_ROLE_IDS
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import InterpolBot


class ActivityCog(commands.Cog):
    def __init__(self, bot: "InterpolBot") -> None:
        self.bot = bot
        self.voice_sessions: dict[int, int] = {}
        self.automated_role_removals: set[int] = set()
        self.check_inactive.start()
        self.check_daily_warnings.start()

    def cog_unload(self) -> None:
        self.check_inactive.cancel()
        self.check_daily_warnings.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        for channel in guild.voice_channels:
            for member in channel.members:
                if not member.bot and member.id not in self.voice_sessions:
                    session_id = await self.bot.database.start_voice_session(member.id, channel.id)
                    if session_id is not None:
                        self.voice_sessions[member.id] = session_id

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message) -> None:
        if message.author.bot or message.guild is None or message.guild.id != GUILD_ID:
            return
        await self.bot.database.record_message_activity(message.author.id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: disnake.Member,
        before: disnake.VoiceState,
        after: disnake.VoiceState,
    ) -> None:
        if member.bot or member.guild.id != GUILD_ID:
            return
        if before.channel is None and after.channel is not None:
            session_id = await self.bot.database.start_voice_session(member.id, after.channel.id)
            if session_id is not None:
                self.voice_sessions[member.id] = session_id
        elif before.channel is not None and after.channel is None:
            session_id = self.voice_sessions.pop(member.id, None)
            if session_id is not None:
                await self.bot.database.end_voice_session(session_id)

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: disnake.Member,
        after: disnake.Member,
    ) -> None:
        if after.guild.id != GUILD_ID or after.id in self.automated_role_removals:
            return
        before_family_roles = [role for role in before.roles if role.id in TARGET_ROLE_IDS]
        after_family_roles = [role for role in after.roles if role.id in TARGET_ROLE_IDS]
        if not before_family_roles or after_family_roles:
            return

        reason = "Сняты семейные роли: " + ", ".join(role.name for role in before_family_roles)
        changed = await self.bot.database.deactivate_user(
            after.id,
            reason,
            roles_removed=True,
        )
        if changed:
            await send_admin_log(
                self.bot,
                title="🧹 Пользователь деактивирован",
                description=(
                    f"Пользователь: {after.mention} (`{after.id}`)\n"
                    f"Причина: **{reason}**"
                ),
                color=disnake.Color.red(),
            )

    @commands.Cog.listener()
    async def on_member_remove(self, member: disnake.Member) -> None:
        if member.guild.id != GUILD_ID:
            return
        profile = await self.bot.database.get_user(member.id)
        if profile is None:
            return
        reason = "Пользователь покинул сервер"
        await self.bot.database.deactivate_user(member.id, reason)
        was_family_member = profile["is_active"] and any(
            role.id in TARGET_ROLE_IDS for role in member.roles
        )
        if not was_family_member:
            return
        await send_admin_log(
            self.bot,
            title="🚪 Пользователь покинул сервер",
            description=(
                f"Пользователь: {member} (`{member.id}`)\n"
                f"Запись сохранена в базе со статусом **Неактивен**.\n"
                f"Причина: **{reason}**"
            ),
            color=disnake.Color.red(),
        )

    @tasks.loop(minutes=1)
    async def check_inactive(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        enforcement_started_at = await self.bot.database.get_activity_enforcement_started_at()
        if disnake.utils.utcnow() < enforcement_started_at + timedelta(days=7):
            return
        for profile in await self.bot.database.get_inactive_users():
            member = guild.get_member(profile["discord_id"])
            if member is None:
                continue
            if member.guild_permissions.administrator:
                continue
            no_messages = profile["last_message_at"] is None or (
                disnake.utils.utcnow() - profile["last_message_at"]
            ).days >= 7
            voice_minutes = profile["weekly_voice_seconds"] // 60
            reasons = []
            if no_messages:
                reasons.append("нет сообщений более 7 дней")
            if profile["weekly_voice_seconds"] < 3600:
                reasons.append(f"голосовая активность за неделю: {voice_minutes} мин. из 60")
            reason = "; ".join(reasons)
            bot_member = guild.me
            removable_roles = [
                role for role in member.roles
                if role != guild.default_role
                and not role.managed
                and bot_member is not None
                and role < bot_member.top_role
            ]
            role_names = ", ".join(role.name for role in removable_roles) or "нет доступных для снятия ролей"
            try:
                self.automated_role_removals.add(member.id)
                try:
                    if removable_roles:
                        await member.remove_roles(*removable_roles, reason=f"Неактивность: {reason}")
                    await self.bot.database.deactivate_user(
                        member.id, f"Неактивность: {reason}", roles_removed=True
                    )
                finally:
                    self.automated_role_removals.discard(member.id)
                await send_admin_log(
                    self.bot,
                    title="🧹 Роли сняты за неактивность",
                    description=f"Пользователь: {member} (`{member.id}`)\nПричина: **{reason}**\nСнятые роли: **{role_names}**",
                    color=disnake.Color.red(),
                )
            except disnake.DiscordException:
                logging.exception("Could not remove roles from inactive member %s", member.id)
                await send_admin_log(
                    self.bot,
                    title="⚠️ Ошибка снятия ролей",
                    description=f"Не удалось снять роли у {member.mention} (`{member.id}`).\nПричина отбора: **{reason}**",
                    color=disnake.Color.orange(),
                )

    @tasks.loop(minutes=1)
    async def check_daily_warnings(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        started_at = await self.bot.database.get_daily_warning_started_at()
        if disnake.utils.utcnow() < started_at + timedelta(days=1):
            return

        for profile in await self.bot.database.get_daily_warning_users():
            member = guild.get_member(profile["discord_id"])
            if member is None or member.bot:
                continue

            no_messages = profile["last_message_at"] is None or (
                disnake.utils.utcnow() - profile["last_message_at"]
            ) >= timedelta(days=1)
            voice_minutes = profile["daily_voice_seconds"] // 60
            reasons: list[str] = []
            if no_messages:
                reasons.append("за последние 24 часа не было сообщений")
            if profile["daily_voice_seconds"] < 1800:
                reasons.append(
                    f"в голосовых каналах проведено {voice_minutes} мин. из необходимых 30"
                )
            reason = "; ".join(reasons)

            embed = disnake.Embed(
                title="⚠️ Напоминание об активности",
                description=(
                    f"Привет, {member.mention}! За последние сутки ваша активность "
                    "оказалась ниже установленной нормы."
                ),
                color=disnake.Color.gold(),
                timestamp=disnake.utils.utcnow(),
            )
            embed.add_field(name="Причина", value=reason, inline=False)
            embed.add_field(
                name="Суточная норма",
                value="💬 Минимум одно сообщение\n🎙️ Минимум 30 минут в voice",
                inline=False,
            )
            embed.set_footer(text="INTERPOL • Контроль активности")

            delivered = True
            try:
                await member.send(embed=embed)
            except disnake.DiscordException:
                delivered = False
                logging.warning("Could not send daily activity warning to %s", member.id)
            await self.bot.database.record_activity_warning(member.id, reason, delivered)

    @check_daily_warnings.before_loop
    async def before_daily_warnings(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)

    @check_inactive.before_loop
    async def before_inactive_check(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)
