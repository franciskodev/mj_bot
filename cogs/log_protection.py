"""Protect this bot's log messages using confirmed Discord audit actions."""
import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import asyncpg
import disnake
from disnake.ext import commands

from config import GUILD_IDS, LOG_MESSAGE_BULK_HISTORY_DAYS, PROTECTED_LOG_CHANNEL_IDS
from log_utils import send_security_log

if TYPE_CHECKING:
    from bot import InterpolovBot


AUDIT_RETRIES = 5
AUDIT_CLOCK_TOLERANCE = timedelta(seconds=5)
DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, RuntimeError)


class LogProtectionCog(commands.Cog):
    def __init__(self, bot: 'InterpolovBot') -> None:
        self.bot = bot
        self.started_at = disnake.utils.utcnow()
        self._audit_counts: dict[int, int] = {}
        self._known_messages: dict[int, tuple[int, int]] = {}
        self._bulk_lock = asyncio.Lock()
        self._history_lock = asyncio.Lock()

    def is_log_channel(self, channel_id: int) -> bool:
        channel = self.bot.get_channel(channel_id)
        return channel_id in PROTECTED_LOG_CHANNEL_IDS or (
            isinstance(channel, disnake.Thread) and channel.parent_id in PROTECTED_LOG_CHANNEL_IDS
        )

    async def remember_messages(self, guild_id: int, channel_id: int, ids: list[int]) -> None:
        if not ids:
            return
        self._known_messages.update((mid, (guild_id, channel_id)) for mid in ids)
        try:
            await self.bot.database.record_protected_log_messages(guild_id, channel_id, ids)
        except DB_ERRORS:
            logging.exception('Could not persist bot message IDs in log channel %s', channel_id)

    @commands.Cog.listener('on_message')
    async def remember_log_message(self, message: disnake.Message) -> None:
        if (
            message.guild is not None and message.guild.id in GUILD_IDS
            and self.bot.user is not None and message.author.id == self.bot.user.id
            and self.is_log_channel(message.channel.id)
        ):
            await self.remember_messages(message.guild.id, message.channel.id, [message.id])

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Bulk deletion only supports recent messages. Older single deletions
        # are identified directly by the author in the audit entry.
        if self._history_lock.locked():
            return
        async with self._history_lock:
            cutoff = disnake.utils.utcnow() - timedelta(days=LOG_MESSAGE_BULK_HISTORY_DAYS + 1)
            before_id = disnake.utils.time_snowflake(cutoff)
            self._known_messages = {mid: scope for mid, scope in self._known_messages.items() if mid >= before_id}
            try:
                await self.bot.database.prune_protected_log_messages(before_id)
            except DB_ERRORS:
                logging.exception('Could not prune old log message IDs')
            for guild_id in GUILD_IDS:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    continue
                # Seed counters for older, possibly aggregated audit entries.
                try:
                    async for entry in guild.audit_logs(limit=100, action=disnake.AuditLogAction.message_delete):
                        if entry.created_at < self.started_at:
                            self._audit_counts.setdefault(entry.id, self.audit_count(entry))
                except disnake.DiscordException:
                    logging.exception('Could not initialize log deletion audit counters')
                channels = [guild.get_channel(cid) for cid in PROTECTED_LOG_CHANNEL_IDS]
                channels.extend(thread for thread in guild.threads if thread.parent_id in PROTECTED_LOG_CHANNEL_IDS)
                for channel in channels:
                    if not isinstance(channel, (disnake.TextChannel, disnake.Thread)):
                        continue
                    batch = []
                    try:
                        async for message in channel.history(limit=None, after=cutoff, oldest_first=False):
                            if self.bot.user is not None and message.author.id == self.bot.user.id:
                                batch.append(message.id)
                            if len(batch) >= 100:
                                await self.remember_messages(guild_id, channel.id, batch)
                                batch = []
                    except disnake.DiscordException:
                        logging.exception('Could not index log history in channel %s', channel.id)
                    await self.remember_messages(guild_id, channel.id, batch)

    @staticmethod
    def audit_count(entry: disnake.AuditLogEntry) -> int:
        return int(getattr(entry.extra, 'count', 0) or 0)

    async def punish_deletion(self, entry, channel_id: int, *, bot_message_count: int) -> None:
        systems = self.bot.get_cog('SystemsCog')
        if systems is None or self.bot.user is None:
            return
        if getattr(entry.user, 'id', None) == self.bot.user.id:
            return
        actor = await systems.security_actor(entry.guild, entry)
        if actor is None or getattr(actor, 'bot', False):
            return
        await systems.remove_security_actor_roles(
            actor,
            event_name='удаление сообщений бота из журнала',
            event_details=(
                f'Канал: <#{channel_id}> (`{channel_id}`).\n'
                f'Удалённых сообщений бота: **{bot_message_count}**.\n'
                f'Запись аудита: `{entry.id}`.'
            ),
            audit_reason='Защита сервера: удаление сообщений бота из лог-канала',
        )

    @commands.Cog.listener('on_audit_log_entry_create')
    async def log_message_delete_audit(self, entry: disnake.AuditLogEntry) -> None:
        if (
            entry.guild.id not in GUILD_IDS or self.bot.user is None
            or entry.action != disnake.AuditLogAction.message_delete
            or getattr(entry.target, 'id', None) != self.bot.user.id
        ):
            return
        channel_id = getattr(getattr(entry.extra, 'channel', None), 'id', None)
        if channel_id is None or not self.is_log_channel(channel_id):
            return
        count = self.audit_count(entry)
        previous = self._audit_counts.get(entry.id)
        self._audit_counts[entry.id] = max(count, previous or 0)
        if previous is None and entry.created_at < self.started_at:
            return
        if count <= (previous or 0):
            return
        # The count is claimed before any await, preventing gateway/REST races.
        await self.punish_deletion(entry, channel_id, bot_message_count=count - (previous or 0))

    @commands.Cog.listener('on_raw_message_delete')
    async def log_message_delete(self, payload: disnake.RawMessageDeleteEvent) -> None:
        if payload.guild_id not in GUILD_IDS or not self.is_log_channel(payload.channel_id):
            return
        cached = payload.cached_message
        if cached is not None and (self.bot.user is None or cached.author.id != self.bot.user.id):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        # Discord may increase the count of an existing deletion audit entry.
        # Read it again instead of attributing an unaudited deletion to the
        # most recent moderator seen in this channel.
        for attempt in range(AUDIT_RETRIES):
            if attempt:
                await asyncio.sleep(1)
            try:
                async for entry in guild.audit_logs(limit=100, action=disnake.AuditLogAction.message_delete):
                    if getattr(getattr(entry.extra, 'channel', None), 'id', None) == payload.channel_id:
                        await self.log_message_delete_audit(entry)
            except disnake.DiscordException:
                logging.exception('Could not inspect log deletion audit in guild %s', guild.id)
                return

    @commands.Cog.listener('on_raw_bulk_message_delete')
    async def log_bulk_message_delete(self, payload: disnake.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id not in GUILD_IDS or not self.is_log_channel(payload.channel_id) or self.bot.user is None:
            return
        observed_at = disnake.utils.utcnow()
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        cached_authors = {message.id: message.author.id for message in payload.cached_messages}
        bot_ids = {mid for mid in payload.message_ids if cached_authors.get(mid) == self.bot.user.id}
        unknown_ids = set(payload.message_ids) - cached_authors.keys()
        bot_ids.update(mid for mid in unknown_ids if self._known_messages.get(mid) == (guild.id, payload.channel_id))
        try:
            bot_ids.update(await self.bot.database.get_protected_log_message_ids(guild.id, payload.channel_id, unknown_ids))
        except DB_ERRORS:
            logging.exception('Could not identify uncached deleted log messages')
        if not bot_ids:
            return
        async with self._bulk_lock:
            for attempt in range(AUDIT_RETRIES):
                if attempt:
                    await asyncio.sleep(1)
                try:
                    matches = []
                    async for entry in guild.audit_logs(limit=100, action=disnake.AuditLogAction.message_bulk_delete):
                        if entry.created_at < observed_at - AUDIT_CLOCK_TOLERANCE:
                            break
                        if (
                            getattr(entry.target, 'id', None) == payload.channel_id
                            and self.audit_count(entry) == len(payload.message_ids)
                        ):
                            matches.append(entry)
                except disnake.DiscordException:
                    logging.exception('Could not inspect bulk log deletion audit')
                    return
                # Discord omits message IDs from bulk audit entries. Do not
                # choose a moderator if several entries could match this batch.
                if len(matches) > 1:
                    break
                if len(matches) == 1 and matches[0].id in self._audit_counts:
                    return
                # Wait for delayed audit records before deciding that a match
                # is unique; an adjacent deletion may initially be the only one.
                if attempt == AUDIT_RETRIES - 1 and len(matches) == 1:
                    entry = matches[0]
                    self._audit_counts[entry.id] = self.audit_count(entry)
                    await self.punish_deletion(entry, payload.channel_id, bot_message_count=len(bot_ids))
                    return
        await send_security_log(
            self.bot,
            title='⚠️ Защита: не удалось подтвердить удалившего логи',
            description=(f'Канал: <#{payload.channel_id}>. Удалённых сообщений бота: **{len(bot_ids)}**.\n'
                         'В журнале аудита нет однозначного совпадения. Автоматическое снятие ролей не выполнялось.'),
        )
