import asyncio
from datetime import datetime, timedelta
import logging
import re
import unicodedata
from typing import TYPE_CHECKING

import asyncpg
import disnake
from disnake.ext import commands, tasks

from access_control import family_role_ids
from cogs.family_ranks import family_rank_from_roles
from config import (
    DEVELOPER_USER_ID, FAMILY_MEMBER_TRIGGER_ROLE_IDS, GUILD_ID,
    MEDIA_ROLE_ID, NICKNAME_GRACE_HOURS, NICKNAME_LOG_CHANNEL_ID,
)
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import InterpolovBot


NICKNAME_FORMATS = {
    "member": "Имя в игре (без фамилии) | Имя в ИРЛ (по желанию) | Статик на мейн-сервере",
    "deputy leader": "Никнейм или Никнейм / ИРЛ имя",
    "leader": "Сервер / Никнейм или Сервер / Никнейм / ИРЛ имя",
}


def nickname_format_kind(role_ids: set[int]) -> str | None:
    rank = family_rank_from_roles(role_ids)
    if rank in {"tech", "curator", "owner"}:
        return None
    return rank if rank in {"deputy leader", "leader"} else "member"


def valid_nickname(nickname: str, kind: str, server_names: set[str]) -> bool:
    nickname = unicodedata.normalize("NFKC", nickname).strip()
    if any(unicodedata.category(char).startswith("C") for char in nickname):
        return False
    if kind == "member":
        parts = [part.strip() for part in nickname.split("|")]
        if len(parts) not in {2, 3}:
            return False
        servers = {unicodedata.normalize("NFKC", server).strip().casefold() for server in server_names}
        # One first name per name block, allowing compound names such as Jean-Luc.
        # Spaces, underscores and extra blocks must not hide a surname or server.
        return all(
            part.casefold() not in servers
            and all(fragment.isalpha() for fragment in re.split(r"[-'’]", part))
            for part in parts[:-1]
        ) and bool(re.fullmatch(r"#?\s*[0-9]+", parts[-1]))
    parts = [part.strip() for part in re.split(r"[|/\\¦]+|\s+[-–—•·]\s+", nickname)]
    if any(not part for part in parts):
        return False

    def name(value: str) -> bool:
        return any(char.isalpha() for char in value)

    if kind == "deputy leader":
        return len(parts) in {1, 2} and all(name(part) for part in parts)
    if kind == "leader":
        servers = {unicodedata.normalize("NFKC", server).strip().casefold() for server in server_names}
        return len(parts) in {2, 3} and parts[0].casefold() in servers and all(name(part) for part in parts[1:])
    return False


class NicknamesCog(commands.Cog):
    def __init__(self, bot: "InterpolovBot") -> None:
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_names: dict[int, str] = {}
        self._penalty_retry_after: dict[int, datetime] = {}
        self.check_nicknames.start()
        self.check_deadlines.start()

    def cog_unload(self) -> None:
        self.check_nicknames.cancel()
        self.check_deadlines.cancel()

    async def log(self, member, title: str, details: str) -> None:
        await send_admin_log(
            self.bot, channel_id=NICKNAME_LOG_CHANNEL_ID, title=title,
            description=f"Участник: {member.mention} (`{member.id}`)\n{details}",
        )

    async def notify(self, member, content: str) -> bool:
        # Nickname enforcement is independent of the optional DM reminder switch.
        try:
            await member.send(content, allowed_mentions=disnake.AllowedMentions.none())
            return True
        except disnake.DiscordException:
            logging.warning("Could not deliver nickname notification to %s", member.id)
            await self.log(member, "⚠️ Уведомление о никнейме не доставлено", content)
            return False

    async def member_format(self, member) -> str | None:
        if member.bot or member.id == DEVELOPER_USER_ID:
            return None
        roles = {role.id for role in member.roles}
        if MEDIA_ROLE_ID in roles:
            return None
        scope = await family_role_ids(self.bot, member.guild.id) | set(FAMILY_MEMBER_TRIGGER_ROLE_IDS)
        if not roles & scope:
            return None
        return nickname_format_kind(roles)

    async def check_member(self, member, *, changed: bool = False) -> None:
        if member.guild.id != GUILD_ID or member.bot:
            return
        try:
            async with self._locks.setdefault(member.id, asyncio.Lock()):
                await self._check_member(member, changed=changed)
        except (disnake.DiscordException, asyncpg.PostgresError, OSError, RuntimeError):
            logging.exception("Nickname check failed for member %s", member.id)

    async def _check_member(self, member, *, changed: bool) -> None:
        database = self.bot.database
        changed = changed and self._last_names.get(member.id) != member.display_name
        self._last_names[member.id] = member.display_name
        # Temporary Mafia role removal must not change the required name format.
        if await database.get_mafia_saved_access(GUILD_ID, member.id):
            return
        warning = await database.get_nickname_warning(GUILD_ID, member.id)
        now = disnake.utils.utcnow()
        if warning is not None and warning["deadline"] <= now and warning["penalized_at"] is None:
            # Recheck the live nickname and roles immediately before any sanction.
            try:
                member = await member.guild.fetch_member(member.id)
            except disnake.NotFound:
                await database.clear_nickname_warning(GUILD_ID, member.id)
                return
        kind = await self.member_format(member)
        if kind is None:
            self._penalty_retry_after.pop(member.id, None)
            if warning is not None:
                await database.clear_nickname_warning(GUILD_ID, member.id)
            return
        servers = {row["name"] for row in await database.get_game_servers(GUILD_ID)}
        if valid_nickname(member.display_name, kind, servers):
            self._penalty_retry_after.pop(member.id, None)
            if warning is not None:
                await database.clear_nickname_warning(GUILD_ID, member.id)
            if warning is not None or changed:
                await self.notify(member, "✅ Никнейм соответствует формату. Таймер исправления сброшен.")
                await self.log(member, "✅ Никнейм соответствует формату", "Таймер исправления сброшен.")
            return

        instructions = f"Нужный формат: **{NICKNAME_FORMATS[kind]}**.\n"
        if kind == "member":
            instructions += (
                "Используйте только разделитель `|`. Укажите имя в игре **без фамилии** "
                "и свой статик на основном сервере. **Название сервера писать не нужно.**\n"
                "Примеры: `Alex | Иван | 12345` или `Alex | 12345`.\n"
            )
        else:
            instructions += "Вместо `/` можно использовать `|`.\n"
        instructions += (
            "**ИРЛ имя — это имя в настоящей жизни. Указывать его необязательно:** "
            "можно полностью пропустить этот блок вместе с лишним разделителем."
        )
        if warning is None:
            deadline = now + timedelta(hours=NICKNAME_GRACE_HOURS)
            delivered = await self.notify(
                member, f"⚠️ Исправьте никнейм на сервере INTERPOLOV. {instructions}\n"
                f"Срок: <t:{int(deadline.timestamp())}:F> ({NICKNAME_GRACE_HOURS} часа). "
                "Если никнейм останется неправильным, ваши доступные боту роли будут сняты.",
            )
            # Do not start a punitive deadline when the user could not be notified.
            if not delivered:
                return
            warning = await database.save_nickname_warning(GUILD_ID, member.id, member.display_name, deadline)
            await self.log(member, "⚠️ Требуется исправить никнейм", f"{instructions}\nСрок: <t:{int(warning['deadline'].timestamp())}:F>.")
        elif changed or member.display_name != warning["nickname"]:
            await database.save_nickname_warning(GUILD_ID, member.id, member.display_name, warning["deadline"])
            await self.notify(
                member, f"⚠️ Новый никнейм всё ещё не соответствует формату. {instructions}\n"
                f"Срок не изменился: <t:{int(warning['deadline'].timestamp())}:F>.",
            )
            await self.log(member, "⚠️ Никнейм изменён неправильно", f"Таймер продолжается до <t:{int(warning['deadline'].timestamp())}:F>.")
        if (
            warning["deadline"] <= now and warning["penalized_at"] is None
            and self._penalty_retry_after.get(member.id, now) <= now
        ):
            await self.remove_roles(member)

    async def remove_roles(self, member) -> None:
        # Retry permission/API failures without flooding the log every minute.
        self._penalty_retry_after[member.id] = disnake.utils.utcnow() + timedelta(hours=1)
        bot_member = member.guild.me
        if (
            member.id in {DEVELOPER_USER_ID, member.guild.owner_id}
            or bot_member is None or not bot_member.guild_permissions.manage_roles
            or member.top_role >= bot_member.top_role
        ):
            await self.log(member, "⚠️ Роли за никнейм не сняты", "Недостаточно прав бота или участник защищён иерархией ролей.")
            return
        roles = [role for role in member.roles if not role.is_default() and not role.managed and role < bot_member.top_role]
        if roles:
            try:
                # One role update prevents intermediate states from restoring famq.
                await member.remove_roles(*roles, reason="Никнейм не исправлен в течение 24 часов", atomic=False)
            except disnake.DiscordException:
                await self.log(member, "⚠️ Ошибка снятия ролей за никнейм", "Discord не позволил снять все роли. Проверка будет повторена.")
                return
        await self.log(member, "🚨 Роли сняты за неправильный никнейм", "Срок исправления истёк.\nСнятые роли: " + (", ".join(role.mention for role in roles) or "нет доступных ролей"))
        await self.notify(member, "Ваши доступные боту роли сняты: никнейм не был исправлен за 24 часа. Исправьте никнейм и обратитесь к администрации.")
        await self.bot.database.mark_nickname_penalty(GUILD_ID, member.id)
        self._penalty_retry_after.pop(member.id, None)

    @commands.Cog.listener()
    async def on_member_update(self, before: disnake.Member, after: disnake.Member) -> None:
        changed = before.display_name != after.display_name
        if changed or {role.id for role in before.roles} != {role.id for role in after.roles}:
            await self.check_member(after, changed=changed)

    @commands.Cog.listener()
    async def on_user_update(self, before: disnake.User, after: disnake.User) -> None:
        if before.display_name == after.display_name:
            return
        guild = self.bot.get_guild(GUILD_ID)
        member = guild.get_member(after.id) if guild is not None else None
        if member is not None and member.nick is None:
            await self.check_member(member, changed=True)

    @tasks.loop(hours=24)
    async def check_nicknames(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is not None:
            for member in list(guild.members):
                await self.check_member(member)

    @tasks.loop(minutes=1)
    async def check_deadlines(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        try:
            for warning in await self.bot.database.get_due_nickname_warnings(GUILD_ID):
                retry_at = self._penalty_retry_after.get(warning["member_id"])
                if retry_at is not None and retry_at > disnake.utils.utcnow():
                    continue
                try:
                    member = guild.get_member(warning["member_id"]) or await guild.fetch_member(warning["member_id"])
                except disnake.NotFound:
                    await self.bot.database.clear_nickname_warning(GUILD_ID, warning["member_id"])
                    continue
                except disnake.DiscordException:
                    logging.exception("Could not fetch nickname deadline member %s", warning["member_id"])
                    continue
                await self.check_member(member)
        except (asyncpg.PostgresError, OSError, RuntimeError):
            logging.exception("Could not load nickname deadlines")

    @check_nicknames.before_loop
    @check_deadlines.before_loop
    async def before_checks(self) -> None:
        await self.bot.wait_until_ready()
