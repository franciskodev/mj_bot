import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands, tasks

from config import (
    ALLOWED_GUILD_IDS,
    DEFAULT_MEMBER_ROLE_ID,
    FAMILY_MEMBER_ROLE_ID,
    FAMILY_ROLE_IDS_FOR_SYNC,
    GUILD_ID,
    TARGET_ROLE_IDS,
)
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import IntoleranceBot


class ActivityCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot
        self.voice_sessions: dict[int, int] = {}
        self.automated_role_removals: set[int] = set()
        self.check_inactive.start()
        self.check_daily_warnings.start()
        self.check_family_roles.start()
        self.sync_family_member_roles.start()

    def cog_unload(self) -> None:
        self.check_inactive.cancel()
        self.check_daily_warnings.cancel()
        self.check_family_roles.cancel()
        self.sync_family_member_roles.cancel()

    async def get_family_role_ids(self) -> set[int]:
        role_ids = set(TARGET_ROLE_IDS)
        config = await self.bot.database.get_application_config(GUILD_ID)
        if config is not None:
            role_ids.update(
                {config["interpolov_role_id"], config["intolerance_role_id"]}
            )
        return role_ids

    @commands.slash_command(
        name="режим_проверки",
        description="Настроить действие бота при проверке активности",
        guild_ids=[GUILD_ID],
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    @commands.has_permissions(administrator=True)
    async def activity_check_mode(
        self,
        inter: disnake.ApplicationCommandInteraction,
        снимать_роли: bool = commands.Param(
            description="Да — снимать роли, нет — только уведомлять в лог"
        ),
    ) -> None:
        await self.bot.database.set_activity_remove_roles(
            inter.guild_id or GUILD_ID, снимать_роли
        )
        mode = "снятие ролей" if снимать_роли else "только уведомления в лог"
        await inter.response.send_message(
            f"✅ Режим проверки активности: **{mode}**.", ephemeral=True
        )

    async def sync_member_family(self, member: disnake.Member) -> None:
        config = await self.bot.database.get_application_config(member.guild.id)
        if config is None:
            return
        member_role_ids = {role.id for role in member.roles}
        families = [
            family_name
            for family_name, role_id in (
                ("INTERPOLOV", config["interpolov_role_id"]),
                ("INTOLERANCE", config["intolerance_role_id"]),
            )
            if role_id in member_role_ids
        ]
        if len(families) == 1:
            await self.bot.database.set_user_family(member.id, families[0])

    async def ensure_family_member_role(self, member: disnake.Member) -> None:
        if member.bot or member.guild.id != GUILD_ID:
            return

        member_role_ids = {role.id for role in member.roles}
        if (
            FAMILY_MEMBER_ROLE_ID in member_role_ids
            or not member_role_ids.intersection(FAMILY_ROLE_IDS_FOR_SYNC)
        ):
            return

        family_member_role = member.guild.get_role(FAMILY_MEMBER_ROLE_ID)
        if family_member_role is None:
            logging.error(
                "Family member role %s was not found in guild %s",
                FAMILY_MEMBER_ROLE_ID,
                member.guild.id,
            )
            return

        try:
            await member.add_roles(
                family_member_role,
                reason="Автоматическая роль участника семьи",
            )
        except disnake.DiscordException:
            logging.exception(
                "Could not add family member role to member %s", member.id
            )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            if guild.id not in ALLOWED_GUILD_IDS:
                continue
            for channel in guild.voice_channels:
                for member in channel.members:
                    if not member.bot and member.id not in self.voice_sessions:
                        session_id = await self.bot.database.start_voice_session(member.id, channel.id)
                        if session_id is not None:
                            self.voice_sessions[member.id] = session_id

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message) -> None:
        if message.author.bot or message.guild is None or message.guild.id not in ALLOWED_GUILD_IDS:
            return
        await self.bot.database.record_message_activity(message.author.id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: disnake.Member,
        before: disnake.VoiceState,
        after: disnake.VoiceState,
    ) -> None:
        if member.bot or member.guild.id not in ALLOWED_GUILD_IDS:
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
        if after.guild.id != GUILD_ID:
            return
        if after.id in self.automated_role_removals:
            return
        await self.ensure_family_member_role(after)
        family_role_ids = await self.get_family_role_ids()
        before_family_roles = [role for role in before.roles if role.id in family_role_ids]
        after_family_roles = [role for role in after.roles if role.id in family_role_ids]
        await self.sync_member_family(after)

        if after.guild_permissions.administrator:
            profile = await self.bot.database.get_user(after.id)
            if profile is None or not profile["is_active"]:
                await self.bot.database.save_users([after], role_ids=family_role_ids)
                profile = await self.bot.database.update_user_profile(
                    after.id,
                    profile_status="Активен",
                    reactivate=True,
                )
                if profile is not None:
                    await send_admin_log(
                        self.bot,
                        title="✅ Администратор добавлен в семью",
                        description=(
                            f"Пользователь: {after.mention} (`{after.id}`)\n"
                            "Причина: **права администратора**"
                        ),
                        color=disnake.Color.green(),
                    )
            return

        if not before_family_roles and after_family_roles:
            await self.bot.database.save_users([after], role_ids=family_role_ids)
            profile = await self.bot.database.update_user_profile(
                after.id,
                profile_status="Активен",
                reactivate=True,
            )
            await self.sync_member_family(after)
            if profile is not None:
                returned_roles = ", ".join(role.name for role in after_family_roles)
                await send_admin_log(
                    self.bot,
                    title="✅ Пользователь реактивирован",
                    description=(
                        f"Пользователь: {after.mention} (`{after.id}`)\n"
                        f"Возвращённые семейные роли: **{returned_roles}**"
                    ),
                    color=disnake.Color.green(),
                )
            return

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
        family_role_ids = await self.get_family_role_ids()
        was_family_member = profile["is_active"] and any(
            role.id in family_role_ids for role in member.roles
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

    @tasks.loop(minutes=40)
    async def sync_family_member_roles(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return

        for member in guild.members:
            await self.ensure_family_member_role(member)

    @tasks.loop(minutes=5)
    async def check_family_roles(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return

        family_role_ids = await self.get_family_role_ids()
        if not family_role_ids:
            return

        for member in guild.members:
            if member.bot or (not member.guild_permissions.administrator and not any(
                role.id in family_role_ids for role in member.roles
            )):
                continue
            profile = await self.bot.database.get_user(member.id)
            if profile is not None and profile["is_active"]:
                await self.sync_member_family(member)
                continue

            await self.bot.database.save_users([member], role_ids=family_role_ids)
            profile = await self.bot.database.update_user_profile(
                member.id,
                profile_status="Активен",
                reactivate=True,
            )
            await self.sync_member_family(member)
            if profile is not None:
                await send_admin_log(
                    self.bot,
                    title="✅ Пользователь реактивирован",
                    description=(
                        f"Пользователь: {member.mention} (`{member.id}`)\n"
                        f"Причина: **{'права администратора' if member.guild_permissions.administrator else 'семейная роль обнаружена при плановой проверке'}**"
                    ),
                    color=disnake.Color.green(),
                )

        for profile in await self.bot.database.get_active_users():
            member = guild.get_member(profile["discord_id"])
            if member is None or member.bot:
                continue
            if member.guild_permissions.administrator or any(
                role.id in family_role_ids for role in member.roles
            ):
                continue

            changed = await self.bot.database.deactivate_user(
                member.id,
                "Семейная роль отсутствует при плановой проверке",
                roles_removed=True,
            )
            if changed:
                await send_admin_log(
                    self.bot,
                    title="🧹 Пользователь деактивирован",
                    description=(
                        f"Пользователь: {member.mention} (`{member.id}`)\n"
                        "Причина: **семейная роль отсутствует при плановой проверке**"
                    ),
                    color=disnake.Color.red(),
                )

    @tasks.loop(minutes=1)
    async def check_inactive(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        enforcement_started_at = await self.bot.database.get_activity_enforcement_started_at(
            GUILD_ID
        )
        if disnake.utils.utcnow() < enforcement_started_at + timedelta(days=7):
            return
        remove_roles = await self.bot.database.get_activity_remove_roles(GUILD_ID)
        for profile in await self.bot.database.get_inactive_users(
            notification_only=not remove_roles
        ):
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
                and role.id != DEFAULT_MEMBER_ROLE_ID
                and not role.managed
                and bot_member is not None
                and role < bot_member.top_role
            ]
            role_names = ", ".join(role.name for role in removable_roles) or "нет доступных для снятия ролей"
            if not remove_roles:
                await self.bot.database.record_inactivity_notice(member.id)
                await send_admin_log(
                    self.bot,
                    title="⚠️ Обнаружена неактивность",
                    description=(
                        f"Пользователь: {member.mention} (`{member.id}`)\n"
                        f"Причина: **{reason}**\n"
                        "Роли не сняты: включён режим уведомлений."
                    ),
                    color=disnake.Color.orange(),
                )
                continue
            try:
                self.automated_role_removals.add(member.id)
                try:
                    if removable_roles:
                        await member.remove_roles(*removable_roles, reason=f"Неактивность: {reason}")
                    inactive_role = (
                        guild.get_role(DEFAULT_MEMBER_ROLE_ID)
                        if DEFAULT_MEMBER_ROLE_ID is not None
                        else None
                    )
                    if inactive_role is None:
                        logging.error(
                            "Inactive role %s was not found in guild %s",
                            DEFAULT_MEMBER_ROLE_ID,
                            guild.id,
                        )
                    elif inactive_role not in member.roles:
                        await member.add_roles(
                            inactive_role,
                            reason=f"Неактивность: {reason}",
                        )
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
        started_at = await self.bot.database.get_daily_warning_started_at(GUILD_ID)
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
            embed.set_footer(text="INTERPOLOV & INTOLERANCE • Контроль активности")

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

    @check_family_roles.before_loop
    async def before_family_roles_check(self) -> None:
        await self.bot.wait_until_ready()

    @sync_family_member_roles.before_loop
    async def before_family_member_roles_sync(self) -> None:
        await self.bot.wait_until_ready()
