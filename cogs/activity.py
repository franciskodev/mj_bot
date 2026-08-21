import asyncio
import logging
from datetime import datetime, timedelta
from time import monotonic
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
from database import BotInstanceLockLost, INSTANCE_LOCK_HEARTBEAT_SECONDS
from log_utils import send_admin_log
from dm_notifications import send_dm_notification

if TYPE_CHECKING:
    from bot import IntoleranceBot


VOICE_RECONCILE_INTERVAL_SECONDS = 60
VOICE_SYNC_MAX_AGE_SECONDS = 150
VOICE_SHUTDOWN_TIMEOUT_SECONDS = 10


class ActivityCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot
        self.voice_sessions: dict[int, int] = {}
        self.voice_session_lock = asyncio.Lock()
        self.voice_gateway_available = False
        self.voice_sessions_recovered = False
        self.voice_gateway_generation = 0
        self.voice_state_version = 0
        self.voice_synced_version = 0
        self.last_voice_sync_monotonic: float | None = None
        self.voice_tracking_stopping = False
        self.voice_shutdown_at: datetime | None = None
        self.voice_shutdown_task: asyncio.Task[datetime] | None = None
        self.automated_role_removals: set[int] = set()
        self.watch_instance_lock.start()
        self.reconcile_voice_activity.start()
        self.check_inactive.start()
        self.check_daily_warnings.start()
        self.check_family_roles.start()
        self.sync_family_member_roles.start()

    def cog_unload(self) -> None:
        self.watch_instance_lock.cancel()
        self.reconcile_voice_activity.cancel()
        self.check_inactive.cancel()
        self.check_daily_warnings.cancel()
        self.check_family_roles.cancel()
        self.sync_family_member_roles.cancel()
        if not self.voice_tracking_stopping:
            self.voice_shutdown_task = asyncio.create_task(
                self.shutdown_voice_activity(),
                name="finalize-voice-activity",
            )

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
    )
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

    @staticmethod
    def eligible_voice_channel_id(
        member: disnake.Member,
        voice: disnake.VoiceState | None,
        afk_channel_id: int | None,
    ) -> int | None:
        if (
            member.bot
            or voice is None
            or voice.channel is None
            or voice.channel.id == afk_channel_id
            or voice.suppress
            or voice.mute
            or voice.self_mute
            or voice.deaf
            or voice.self_deaf
        ):
            return None
        return voice.channel.id

    def current_voice_candidates(
        self,
        *,
        voice_override: tuple[disnake.Member, disnake.VoiceState] | None = None,
    ) -> dict[int, int] | None:
        """Return voice presences eligible for the active-family quorum."""
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None or guild.unavailable:
            return None

        voice_candidates: dict[int, int] = {}
        afk_channel_id = (
            guild.afk_channel.id if guild.afk_channel is not None else None
        )
        channels = (*guild.voice_channels, *guild.stage_channels)
        for channel in channels:
            for member in channel.members:
                channel_id = self.eligible_voice_channel_id(
                    member, member.voice, afk_channel_id
                )
                if channel_id == channel.id:
                    voice_candidates[member.id] = channel_id

        if voice_override is not None:
            member, voice = voice_override
            voice_candidates.pop(member.id, None)
            channel_id = self.eligible_voice_channel_id(
                member, voice, afk_channel_id
            )
            if channel_id is not None:
                voice_candidates[member.id] = channel_id
        return voice_candidates

    def mark_voice_sync_unhealthy(self) -> None:
        self.voice_sessions_recovered = False
        self.last_voice_sync_monotonic = None

    def voice_tracking_is_fresh(self, generation: int | None = None) -> bool:
        last_sync = self.last_voice_sync_monotonic
        return (
            not self.voice_tracking_stopping
            and self.voice_gateway_available
            and self.voice_sessions_recovered
            and (generation is None or generation == self.voice_gateway_generation)
            and self.voice_synced_version >= self.voice_state_version
            and last_sync is not None
            and monotonic() - last_sync <= VOICE_SYNC_MAX_AGE_SECONDS
        )

    async def sync_current_voice_activity(
        self,
        *,
        recover: bool | None = None,
        required_version: int | None = None,
        voice_candidates: dict[int, int] | None = None,
        observed_at: datetime | None = None,
        observed_monotonic: float | None = None,
    ) -> bool:
        async with self.voice_session_lock:
            if self.voice_tracking_stopping or not self.voice_gateway_available:
                return False
            if (
                required_version is not None
                and self.voice_sessions_recovered
                and self.voice_synced_version >= required_version
            ):
                return True
            if recover is None:
                recover = not self.voice_sessions_recovered
            generation = self.voice_gateway_generation
            snapshot_version = (
                required_version
                if required_version is not None
                else self.voice_state_version
            )
            try:
                if observed_at is None:
                    observed_at = disnake.utils.utcnow()
                if observed_monotonic is None:
                    observed_monotonic = monotonic()
                current_members = (
                    voice_candidates
                    if voice_candidates is not None
                    else self.current_voice_candidates()
                )
                if current_members is None:
                    self.mark_voice_sync_unhealthy()
                    return False
                sessions = await self.bot.database.reconcile_voice_sessions(
                    current_members,
                    observed_at=observed_at,
                    recover=recover,
                )
            except BotInstanceLockLost as error:
                self.mark_voice_sync_unhealthy()
                self.bot.request_fatal_shutdown(error)
                raise
            except Exception:
                self.mark_voice_sync_unhealthy()
                raise
            if (
                not self.voice_gateway_available
                or generation != self.voice_gateway_generation
            ):
                self.mark_voice_sync_unhealthy()
                return False
            self.voice_sessions = sessions
            self.voice_sessions_recovered = True
            self.voice_synced_version = max(
                self.voice_synced_version,
                snapshot_version,
            )
            self.last_voice_sync_monotonic = observed_monotonic
            return True

    async def ensure_voice_tracking_fresh(
        self,
        *,
        expected_generation: int | None = None,
        force: bool = False,
    ) -> int | None:
        if (
            expected_generation is not None
            and expected_generation != self.voice_gateway_generation
        ):
            return None
        if force or not self.voice_tracking_is_fresh(expected_generation):
            try:
                if not await self.sync_current_voice_activity():
                    return None
            except Exception:
                logging.exception("Could not refresh voice activity before enforcement")
                return None
        generation = self.voice_gateway_generation
        if expected_generation is not None and generation != expected_generation:
            return None
        return generation if self.voice_tracking_is_fresh(generation) else None

    async def reconcile_voice_eligibility_change(self) -> None:
        if not self.voice_gateway_available or self.voice_tracking_stopping:
            return
        try:
            await self.sync_current_voice_activity()
        except Exception:
            logging.exception("Could not reconcile changed voice eligibility")

    async def restore_voice_activity(self) -> None:
        if self.voice_tracking_stopping:
            return
        self.voice_gateway_available = True
        try:
            await self.sync_current_voice_activity()
        except Exception:
            logging.exception("Could not restore voice activity tracking")

    def mark_voice_gateway_unavailable(self) -> None:
        self.voice_gateway_generation += 1
        self.voice_gateway_available = False
        self.mark_voice_sync_unhealthy()
        self.voice_sessions.clear()

    async def shutdown_voice_activity(self) -> datetime:
        if self.voice_shutdown_at is not None:
            return self.voice_shutdown_at

        shutdown_at = disnake.utils.utcnow()
        self.voice_shutdown_at = shutdown_at
        if (
            self.bot.voice_shutdown_at is None
            or shutdown_at < self.bot.voice_shutdown_at
        ):
            self.bot.voice_shutdown_at = shutdown_at
        self.voice_tracking_stopping = True
        self.mark_voice_gateway_unavailable()
        self.watch_instance_lock.cancel()
        self.reconcile_voice_activity.cancel()

        async def finalize_sessions() -> None:
            async with self.voice_session_lock:
                await self.bot.database.close_voice_sessions(shutdown_at)

        try:
            await asyncio.wait_for(
                finalize_sessions(),
                timeout=VOICE_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except Exception:
            logging.exception("Could not finalize voice sessions during shutdown")
        return shutdown_at

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.restore_voice_activity()

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        await self.restore_voice_activity()

    @commands.Cog.listener()
    async def on_disconnect(self) -> None:
        self.mark_voice_gateway_unavailable()

    @commands.Cog.listener()
    async def on_guild_available(self, guild: disnake.Guild) -> None:
        if guild.id == GUILD_ID:
            await self.restore_voice_activity()

    @commands.Cog.listener()
    async def on_guild_unavailable(self, guild: disnake.Guild) -> None:
        if guild.id == GUILD_ID:
            self.mark_voice_gateway_unavailable()

    @commands.Cog.listener()
    async def on_voice_eligibility_changed(self) -> None:
        await self.reconcile_voice_eligibility_change()

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
        if member.bot or member.guild.id != GUILD_ID:
            return
        before_state = (
            before.channel.id if before.channel is not None else None,
            before.suppress,
            before.mute,
            before.self_mute,
            before.deaf,
            before.self_deaf,
        )
        after_state = (
            after.channel.id if after.channel is not None else None,
            after.suppress,
            after.mute,
            after.self_mute,
            after.deaf,
            after.self_deaf,
        )
        if before_state == after_state:
            return
        if not self.voice_gateway_available:
            return
        self.voice_state_version += 1
        required_version = self.voice_state_version
        try:
            observed_at = disnake.utils.utcnow()
            observed_monotonic = monotonic()
            voice_candidates = self.current_voice_candidates(
                voice_override=(member, after)
            )
            if voice_candidates is None:
                self.mark_voice_sync_unhealthy()
                return
            await self.sync_current_voice_activity(
                required_version=required_version,
                voice_candidates=voice_candidates,
                observed_at=observed_at,
                observed_monotonic=observed_monotonic,
            )
        except Exception:
            self.mark_voice_sync_unhealthy()
            logging.exception(
                "Could not reconcile voice activity after update for member %s",
                member.id,
            )

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
                await self.reconcile_voice_eligibility_change()
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

        if (
            before.guild_permissions.administrator
            and not after.guild_permissions.administrator
            and not after_family_roles
        ):
            changed = await self.bot.database.deactivate_user(
                after.id,
                "Сняты права администратора, семейная роль отсутствует",
            )
            if changed:
                await send_admin_log(
                    self.bot,
                    title="🧹 Пользователь деактивирован",
                    description=(
                        f"Пользователь: {after.mention} (`{after.id}`)\n"
                        "Причина: **сняты права администратора, семейная роль отсутствует**"
                    ),
                    color=disnake.Color.red(),
                )
            return

        if not before_family_roles and after_family_roles:
            await self.bot.database.save_users([after], role_ids=family_role_ids)
            profile = await self.bot.database.update_user_profile(
                after.id,
                profile_status="Активен",
                reactivate=True,
            )
            await self.reconcile_voice_eligibility_change()
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

    @tasks.loop(seconds=INSTANCE_LOCK_HEARTBEAT_SECONDS)
    async def watch_instance_lock(self) -> None:
        try:
            await self.bot.database.ensure_instance_lock(force=True)
        except asyncio.CancelledError:
            raise
        except BotInstanceLockLost as error:
            self.mark_voice_sync_unhealthy()
            self.bot.request_fatal_shutdown(error)

    @tasks.loop(seconds=VOICE_RECONCILE_INTERVAL_SECONDS)
    async def reconcile_voice_activity(self) -> None:
        if not self.voice_gateway_available:
            return
        try:
            await self.sync_current_voice_activity()
        except Exception:
            logging.exception("Could not reconcile voice activity")

    @tasks.loop(minutes=40)
    async def sync_family_member_roles(self) -> None:
        try:
            await self.run_family_member_role_sync()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Family-member role sync failed")

    async def run_family_member_role_sync(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return

        for member in guild.members:
            await self.ensure_family_member_role(member)

    @tasks.loop(minutes=5)
    async def check_family_roles(self) -> None:
        try:
            await self.run_family_role_check()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Family-role check failed")

    async def run_family_role_check(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return

        family_role_ids = await self.get_family_role_ids()
        if not family_role_ids:
            return

        for member in guild.members:
            if (
                member.bot
                or member.id in self.automated_role_removals
                or (
                    not member.guild_permissions.administrator
                    and not any(role.id in family_role_ids for role in member.roles)
                )
            ):
                continue
            profile = await self.bot.database.get_user(member.id)
            if profile is not None and profile["is_active"]:
                await self.sync_member_family(member)
                continue
            if (
                profile is not None
                and not member.guild_permissions.administrator
                and profile["roles_removed_at"] is None
            ):
                # An administrative deactivation must not be undone merely because
                # the old family role is still present. Explicitly adding a role is
                # handled by on_member_update; automated removals have a timestamp.
                continue
            if profile is not None and not member.guild_permissions.administrator:
                try:
                    member = await guild.fetch_member(member.id)
                except disnake.DiscordException:
                    logging.exception(
                        "Could not verify family roles for member %s", member.id
                    )
                    continue
                if not any(role.id in family_role_ids for role in member.roles):
                    continue

            await self.bot.database.save_users([member], role_ids=family_role_ids)
            profile = await self.bot.database.update_user_profile(
                member.id,
                profile_status="Активен",
                reactivate=True,
            )
            await self.reconcile_voice_eligibility_change()
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
            try:
                member = await guild.fetch_member(member.id)
            except disnake.DiscordException:
                logging.exception(
                    "Could not verify missing family roles for member %s",
                    member.id,
                )
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
        try:
            await self.run_inactive_check()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Inactive-member check failed")

    async def run_inactive_check(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None or guild.unavailable:
            return
        generation = await self.ensure_voice_tracking_fresh(force=True)
        if generation is None:
            return
        enforcement_started_at = await self.bot.database.get_activity_enforcement_started_at(
            GUILD_ID
        )
        if disnake.utils.utcnow() < enforcement_started_at + timedelta(days=7):
            return
        remove_roles = await self.bot.database.get_activity_remove_roles(GUILD_ID)
        family_role_ids = await self.get_family_role_ids() if remove_roles else set()
        for profile in await self.bot.database.get_inactive_users(
            notification_only=not remove_roles
        ):
            member = guild.get_member(profile["discord_id"])
            if member is None:
                continue
            if member.guild_permissions.administrator:
                continue
            if await self.ensure_voice_tracking_fresh(
                expected_generation=generation,
            ) is None:
                return
            refreshed_profiles = await self.bot.database.get_inactive_users(
                notification_only=not remove_roles,
                discord_id=member.id,
            )
            if not refreshed_profiles:
                continue
            if not self.voice_tracking_is_fresh(generation):
                return
            profile = refreshed_profiles[0]
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
                member = await guild.fetch_member(member.id)
            except disnake.DiscordException:
                logging.exception(
                    "Could not fetch current roles for inactive member %s",
                    member.id,
                )
                continue
            if member.guild_permissions.administrator:
                continue
            if await self.ensure_voice_tracking_fresh(
                expected_generation=generation,
            ) is None:
                return
            final_profiles = await self.bot.database.get_inactive_users(
                notification_only=False,
                discord_id=member.id,
            )
            if not final_profiles:
                continue
            if not self.voice_tracking_is_fresh(generation):
                return
            profile = final_profiles[0]
            no_messages = profile["last_message_at"] is None or (
                disnake.utils.utcnow() - profile["last_message_at"]
            ).days >= 7
            voice_minutes = profile["weekly_voice_seconds"] // 60
            reasons = []
            if no_messages:
                reasons.append("нет сообщений более 7 дней")
            if profile["weekly_voice_seconds"] < 3600:
                reasons.append(
                    f"голосовая активность за неделю: {voice_minutes} мин. из 60"
                )
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
            role_names = (
                ", ".join(role.name for role in removable_roles)
                or "нет доступных для снятия ролей"
            )
            unremovable_family_roles = [
                role
                for role in member.roles
                if role.id in family_role_ids and role not in removable_roles
            ]
            if unremovable_family_roles:
                role_list = ", ".join(role.name for role in unremovable_family_roles)
                logging.error(
                    "Family roles cannot be removed from member %s: %s",
                    member.id,
                    role_list,
                )
                await send_admin_log(
                    self.bot,
                    title="⚠️ Семейные роли недоступны для снятия",
                    description=(
                        f"Профиль {member.mention} (`{member.id}`) не деактивирован. "
                        f"Переместите роли ниже роли бота: **{role_list}**."
                    ),
                    color=disnake.Color.orange(),
                )
                continue

            inactive_role = (
                guild.get_role(DEFAULT_MEMBER_ROLE_ID)
                if DEFAULT_MEMBER_ROLE_ID is not None
                else None
            )
            bot_member = guild.me
            if inactive_role is None or (
                inactive_role not in member.roles
                and (
                    inactive_role.managed
                    or bot_member is None
                    or inactive_role >= bot_member.top_role
                )
            ):
                logging.error(
                    "Inactive role %s is missing or cannot be assigned in guild %s",
                    DEFAULT_MEMBER_ROLE_ID,
                    guild.id,
                )
                await send_admin_log(
                    self.bot,
                    title="⚠️ Проверка активности остановлена",
                    description=(
                        f"Роли у {member.mention} (`{member.id}`) не снимались: "
                        "бот не может выдать роль неактивного участника."
                    ),
                    color=disnake.Color.orange(),
                )
                continue

            if not self.voice_tracking_is_fresh(generation):
                return
            desired_roles = [
                role
                for role in member.roles
                if role != guild.default_role and role not in removable_roles
            ]
            if inactive_role not in desired_roles:
                desired_roles.append(inactive_role)
            self.automated_role_removals.add(member.id)
            try:
                transition_stage = "roles"

                async def apply_inactive_transition() -> None:
                    nonlocal transition_stage
                    if removable_roles or inactive_role not in member.roles:
                        await member.edit(
                            roles=desired_roles,
                            reason=f"Неактивность: {reason}",
                        )
                    transition_stage = "database"
                    await self.bot.database.deactivate_user(
                        member.id,
                        f"Неактивность: {reason}",
                        roles_removed=True,
                    )

                transition_task = asyncio.create_task(
                    apply_inactive_transition(),
                    name=f"deactivate-inactive-member-{member.id}",
                )
                try:
                    await asyncio.shield(transition_task)
                except asyncio.CancelledError:
                    # Once the single Discord role PATCH has started, finish the
                    # matching database transition before allowing cog shutdown.
                    try:
                        await transition_task
                    except Exception:
                        logging.exception(
                            "Inactive transition for member %s failed during shutdown",
                            member.id,
                        )
                    raise
                except disnake.DiscordException:
                    logging.exception(
                        "Could not update roles for inactive member %s", member.id
                    )
                    await send_admin_log(
                        self.bot,
                        title="⚠️ Ошибка снятия ролей",
                        description=(
                            f"Не удалось снять роли у {member.mention} (`{member.id}`).\n"
                            f"Причина отбора: **{reason}**"
                        ),
                        color=disnake.Color.orange(),
                    )
                    continue
                except Exception as error:
                    if isinstance(error, BotInstanceLockLost):
                        self.bot.request_fatal_shutdown(error)
                    logging.exception(
                        "%s transition failed for inactive member %s",
                        transition_stage.capitalize(),
                        member.id,
                    )
                    await send_admin_log(
                        self.bot,
                        title="⚠️ Требуется повторная деактивация",
                        description=(
                            f"Роли у {member.mention} (`{member.id}`) сняты, но БД "
                            "временно не подтвердила деактивацию. Бот повторит проверку."
                        ),
                        color=disnake.Color.orange(),
                    )
                    continue
                await send_admin_log(
                    self.bot,
                    title="🧹 Роли сняты за неактивность",
                    description=f"Пользователь: {member} (`{member.id}`)\nПричина: **{reason}**\nСнятые роли: **{role_names}**",
                    color=disnake.Color.red(),
                )
            finally:
                self.automated_role_removals.discard(member.id)

    @tasks.loop(minutes=1)
    async def check_daily_warnings(self) -> None:
        try:
            await self.run_daily_warning_check()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Daily activity warning check failed")

    async def run_daily_warning_check(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None or guild.unavailable:
            return
        generation = await self.ensure_voice_tracking_fresh(force=True)
        if generation is None:
            return
        started_at = await self.bot.database.get_daily_warning_started_at(GUILD_ID)
        if disnake.utils.utcnow() < started_at + timedelta(days=1):
            return

        for profile in await self.bot.database.get_daily_warning_users():
            member = guild.get_member(profile["discord_id"])
            if member is None or member.bot:
                continue
            if await self.ensure_voice_tracking_fresh(
                expected_generation=generation,
            ) is None:
                return
            refreshed_profiles = await self.bot.database.get_daily_warning_users(
                discord_id=member.id
            )
            if not refreshed_profiles:
                continue
            if not self.voice_tracking_is_fresh(generation):
                return
            profile = refreshed_profiles[0]

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

            delivery = await send_dm_notification(
                self.bot,
                member,
                context="напоминание об активности",
                embed=embed,
            )
            delivered = delivery.delivered
            await self.bot.database.record_activity_warning(member.id, reason, delivered)

    @check_daily_warnings.before_loop
    async def before_daily_warnings(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)

    @watch_instance_lock.before_loop
    async def before_instance_lock_watch(self) -> None:
        await self.bot.wait_until_ready()

    @reconcile_voice_activity.before_loop
    async def before_voice_activity_reconciliation(self) -> None:
        await self.bot.wait_until_ready()

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
