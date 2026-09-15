import asyncio
import logging
import re
import unicodedata
from datetime import timedelta
from urllib.parse import unquote

import disnake
from disnake.ext import commands

from access_control import member_access_level
from config import DISCORD_LINK_WARNING_WINDOW_HOURS, GUILD_ID
from log_utils import send_security_log


INVITE_RE = re.compile(
    r"(?<![\w.-])(?:www\.|ptb\.|canary\.)?"
    r"(?:discord\.gg/|discord(?:app)?\.com/invite/)([\w-]+)",
    re.IGNORECASE | re.ASCII,
)
CHANNEL_RE = re.compile(
    r"(?<![\w.-])(?:www\.|ptb\.|canary\.)?"
    r"discord(?:app)?\.com/channels/(\d+)(?=[/?#\s>)\]]|$)",
    re.IGNORECASE,
)
WARNING_WINDOW = timedelta(hours=DISCORD_LINK_WARNING_WINDOW_HOURS)


def discord_links(content: str) -> tuple[set[str], set[int]]:
    content = "".join(
        char for char in unquote(content)
        if unicodedata.category(char) != "Cf"
    )
    return set(INVITE_RE.findall(content)), {
        int(guild_id) for guild_id in CHANNEL_RE.findall(content)
    }


class InviteFilterCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message) -> None:
        await self.check_message(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: disnake.RawMessageUpdateEvent) -> None:
        # Raw events also cover messages sent before startup or evicted from cache.
        if payload.guild_id != GUILD_ID or "content" not in payload.data:
            return
        codes, guild_ids = discord_links(payload.data["content"])
        if not codes and not guild_ids:
            return
        try:
            channel = self.bot.get_channel(payload.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
        except disnake.DiscordException:
            logging.exception("Could not fetch edited message %s", payload.message_id)
            return
        await self.check_message(message)

    async def check_message(self, message: disnake.Message) -> None:
        if (
            message.guild is None or message.guild.id != GUILD_ID
            or message.webhook_id is not None
            or not isinstance(message.author, disnake.Member)
        ):
            return
        codes, guild_ids = discord_links(message.content)
        if not codes and not guild_ids:
            return
        member = message.author
        async with self._locks.setdefault(member.id, asyncio.Lock()):
            systems = self.bot.get_cog("SystemsCog")
            if systems is None:
                logging.error("Invite filter requires SystemsCog")
                return
            if (
                systems.is_current_bot(member)
                or await systems.security_exemption_reason(member) is not None
                or await member_access_level(self.bot, member) >= 6
            ):
                return

            external = bool(guild_ids - {GUILD_ID})
            unresolved = False
            for code in sorted(codes):
                if external:
                    break
                try:
                    invite = await self.bot.fetch_invite(code, with_counts=False)
                except disnake.DiscordException:
                    # Delete unverifiable invitations, but never strip roles for
                    # an expired invite or a temporary Discord lookup failure.
                    unresolved = True
                    continue
                if invite.guild is not None and invite.guild.id != GUILD_ID:
                    external = True
                elif invite.guild is None:
                    unresolved = True
            if not external and not unresolved:
                return

            deleted = True
            try:
                await message.delete()
            except disnake.NotFound:
                return
            except disnake.DiscordException:
                deleted = False
                logging.exception("Could not delete Discord link message %s", message.id)
            details = (
                f"Канал: <#{message.channel.id}>\nСообщение: `{message.id}`\n"
                f"Удаление сообщения: {'успешно' if deleted else 'ошибка Discord'}."
            )
            if not external:
                await send_security_log(
                    self.bot, title="⚠️ Не удалось проверить приглашение Discord",
                    description=f"Участник: {member.mention}\n{details}\nНарушение не засчитано.",
                    color=disnake.Color.orange(),
                )
                return
            now = disnake.utils.utcnow()
            try:
                repeated = await self.bot.database.record_discord_link_violation(
                    GUILD_ID, member.id, message.id, now, now - WARNING_WINDOW,
                )
            except Exception:
                logging.exception("Could not record Discord link violation for %s", member.id)
                await send_security_log(
                    self.bot, title="⚠️ Ошибка учёта ссылок Discord",
                    description=f"Участник: {member.mention}\n{details}\nБаза недоступна; роли не сняты.",
                )
                return
            if repeated is None:
                return
            if repeated:
                await systems.remove_security_actor_roles(
                    member, event_name="повторная ссылка на другой Discord-сервер",
                    event_details=details,
                    audit_reason="Повторная ссылка на другой Discord-сервер за последние 12 часов",
                )
                return
            warning = (
                f"{member.mention}, предупреждение: ссылки на другие Discord-серверы запрещены. "
                f"При повторном нарушении в течение {DISCORD_LINK_WARNING_WINDOW_HOURS} часов "
                "бот снимет доступные ему роли."
            )
            try:
                await message.channel.send(
                    warning, allowed_mentions=disnake.AllowedMentions(
                        users=[member], roles=False, everyone=False, replied_user=False,
                    ),
                )
            except disnake.DiscordException:
                try:
                    await member.send(warning, allowed_mentions=disnake.AllowedMentions.none())
                except disnake.DiscordException:
                    logging.exception("Could not deliver Discord link warning to %s", member.id)
            await send_security_log(
                self.bot, title="⚠️ Предупреждение за ссылку на другой Discord-сервер",
                description=f"Участник: {member.mention}\n{details}",
                color=disnake.Color.orange(),
            )
