import asyncio
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import disnake

from cogs.systems import SystemsCog
from config import ADMIN_LOG_CHANNEL_ID, SECURITY_LOG_CHANNEL_ID, DEVELOPER_USER_ID, GUILD_ID
from log_utils import send_security_log


class Role:
    def __init__(self, role_id, position, managed=False):
        self.id, self.position, self.managed = role_id, position, managed
        self.mention = f'<@&{role_id}>'

    def is_default(self):
        return self.position == 0

    def __lt__(self, other):
        return self.position < other.position

    def __gt__(self, other):
        return self.position > other.position


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.everyone = Role(GUILD_ID, 0)
        self.role = Role(101, 1)
        self.staff_role = Role(102, 2)
        self.guild = SimpleNamespace(id=GUILD_ID, owner_id=999)
        self.guild.me = self.member(900, [Role(900, 10)])
        self.actor = self.member(1, [self.staff_role])
        self.before = self.member(2, [])
        self.after = self.member(2, [self.role])
        self.guild.get_member = Mock(side_effect=lambda mid: {1: self.actor, 900: self.guild.me}.get(mid))
        self.guild.fetch_member = AsyncMock(return_value=None)
        self.database = SimpleNamespace(
            record_security_role_removals=AsyncMock(),
            get_security_role_removals=AsyncMock(return_value=[]),
            clear_security_role_removals=AsyncMock(),
        )
        self.bot = SimpleNamespace(database=self.database)
        self.cog = SystemsCog(self.bot)
        self.log = AsyncMock()
        self.patcher = patch('cogs.systems.send_security_log', self.log)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.now = disnake.utils.utcnow()

    def member(self, member_id, roles):
        return SimpleNamespace(
            id=member_id, guild=self.guild, roles=[self.everyone, *roles],
            top_role=max([self.everyone, *roles], key=lambda r: r.position),
            mention=f'<@{member_id}>', remove_roles=AsyncMock(),
            guild_permissions=disnake.Permissions(manage_roles=True),
        )

    def entry(self, entry_id=123, actor_id=1, roles=None, timestamp=None):
        return SimpleNamespace(
            id=entry_id, user=SimpleNamespace(id=actor_id), target=SimpleNamespace(id=2),
            before=SimpleNamespace(roles=[]), after=SimpleNamespace(roles=roles or [self.role]),
            created_at=timestamp or self.now,
        )

    def prepare_restore(self, age=timedelta(hours=1), entry=None):
        self.database.get_security_role_removals.return_value = [
            {'role_id': self.role.id, 'removed_at': self.now - age},
        ]
        self.cog.find_security_audit_entry = AsyncMock(side_effect=[entry or self.entry(), None])

    async def punish(self, member=None):
        await self.cog.remove_security_actor_roles(
            member or self.actor, event_name='удаление канала', event_details='test', audit_reason='test',
        )

    async def test_successful_removal_is_recorded_and_logged(self):
        await self.punish()
        self.actor.remove_roles.assert_awaited_once()
        args = self.database.record_security_role_removals.call_args.args
        self.assertEqual(args[:3], (GUILD_ID, 1, [102]))
        self.assertEqual(self.cog._security_role_removals[(GUILD_ID, 1)], [(102, args[3])])
        self.assertIn('Снятые роли', self.log.call_args.kwargs['description'])

    async def test_failed_removal_does_not_create_punishment_record(self):
        self.actor.remove_roles.side_effect = disnake.DiscordException('denied')
        with self.assertLogs(level='ERROR'):
            await self.punish()
        self.database.record_security_role_removals.assert_not_awaited()
        self.assertFalse(self.cog._security_role_removals)
        self.assertIn('Ошибка защиты', self.log.call_args.kwargs['title'])

    async def test_database_failure_keeps_memory_protection(self):
        self.database.record_security_role_removals.side_effect = OSError('offline')
        self.database.get_security_role_removals.side_effect = OSError('offline')
        with self.assertLogs(level='ERROR'):
            await self.cog.record_security_role_removals(self.after, [self.role], self.now - timedelta(minutes=1))
        self.cog.find_security_audit_entry = AsyncMock(return_value=self.entry())
        with self.assertLogs(level='ERROR'):
            await self.cog.security_member_role_restore(self.before, self.after)
        self.actor.remove_roles.assert_awaited_once()

    async def test_restorer_loses_roles_and_also_becomes_protected_from_restoration(self):
        self.prepare_restore()
        await self.cog.security_member_role_restore(self.before, self.after)
        self.actor.remove_roles.assert_awaited_once()
        self.assertEqual(self.database.record_security_role_removals.call_args.args[:3], (GUILD_ID, 1, [102]))
        self.after.remove_roles.assert_not_awaited()

    async def reset_role_timer(self):
        inter = SimpleNamespace(
            guild_id=GUILD_ID, author=self.member(DEVELOPER_USER_ID, []),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
        )
        await SystemsCog.role_timer_reset.callback(self.cog, inter, self.after)
        return inter

    async def test_timer_reset_clears_memory_and_persistence_only_for_recipient(self):
        records = [(self.role.id, self.now - timedelta(minutes=1))]
        self.cog._security_role_removals[(GUILD_ID, self.after.id)] = records.copy()
        self.cog._security_role_removals[(GUILD_ID, self.actor.id)] = records.copy()
        self.prepare_restore()

        async def clear(guild_id, member_id):
            self.database.get_security_role_removals.return_value = []

        self.database.clear_security_role_removals.side_effect = clear
        await self.reset_role_timer()
        self.database.clear_security_role_removals.assert_awaited_once_with(GUILD_ID, self.after.id)
        self.assertNotIn((GUILD_ID, self.after.id), self.cog._security_role_removals)
        self.assertEqual(self.cog._security_role_removals[(GUILD_ID, self.actor.id)], records)
        await self.cog.security_member_role_restore(self.before, self.after)
        self.actor.remove_roles.assert_not_awaited()
        self.cog.find_security_audit_entry.assert_not_awaited()
        # A fresh cog must not reload the cleared timer from storage.
        self.cog = SystemsCog(self.bot)
        await self.cog.security_member_role_restore(self.before, self.after)
        self.actor.remove_roles.assert_not_awaited()

    async def test_failed_timer_reset_preserves_protection(self):
        records = [(self.role.id, self.now - timedelta(minutes=1))]
        self.cog._security_role_removals[(GUILD_ID, self.after.id)] = records.copy()
        self.database.clear_security_role_removals.side_effect = OSError('offline')
        with self.assertLogs(level='ERROR'):
            inter = await self.reset_role_timer()
        self.assertIn('Не удалось', inter.edit_original_response.call_args.kwargs['content'])
        self.assertEqual(self.cog._security_role_removals[(GUILD_ID, self.after.id)], records)
        self.cog.find_security_audit_entry = AsyncMock(return_value=self.entry())
        await self.cog.security_member_role_restore(self.before, self.after)
        self.actor.remove_roles.assert_awaited_once()

    async def test_new_punishment_after_reset_starts_protection_again(self):
        await self.reset_role_timer()
        await self.cog.record_security_role_removals(
            self.after, [self.role], self.now - timedelta(seconds=1),
        )
        self.cog.find_security_audit_entry = AsyncMock(return_value=self.entry())
        await self.cog.security_member_role_restore(self.before, self.after)
        self.actor.remove_roles.assert_awaited_once()

    async def test_timer_reset_cancels_restore_check_waiting_for_audit(self):
        self.prepare_restore()

        async def find_entry(*args, **kwargs):
            await self.reset_role_timer()
            return self.entry()

        self.cog.find_security_audit_entry.side_effect = find_entry
        await self.cog.security_member_role_restore(self.before, self.after)
        self.actor.remove_roles.assert_not_awaited()

    async def test_timer_reset_waits_for_inflight_punishment_record(self):
        lock = self.cog._security_locks.setdefault(self.after.id, asyncio.Lock())
        async with lock:
            task = asyncio.create_task(self.reset_role_timer())
            await asyncio.sleep(0)
            self.database.clear_security_role_removals.assert_not_awaited()
            await self.cog.record_security_role_removals(self.after, [self.role], self.now)
        await task
        self.assertNotIn((GUILD_ID, self.after.id), self.cog._security_role_removals)

    async def test_time_window_uses_audit_time_with_strict_three_hour_boundary(self):
        for age, expected in ((timedelta(hours=3, seconds=-1), True),
                              (timedelta(hours=3), False), (timedelta(hours=4), False),
                              (timedelta(seconds=-1), False)):
            with self.subTest(age=age):
                self.cog._processed_role_restoration_entries.clear()
                self.actor.remove_roles.reset_mock()
                self.prepare_restore(age)
                await self.cog.security_member_role_restore(self.before, self.after)
                self.assertEqual(self.actor.remove_roles.await_count, int(expected))

    async def test_bot_developer_owner_and_higher_role_are_exempt(self):
        for member in (self.guild.me, self.member(DEVELOPER_USER_ID, [self.staff_role]),
                       self.member(999, [self.staff_role]), self.member(1, [Role(888, 11)])):
            with self.subTest(member=member.id):
                self.cog._processed_role_restoration_entries.clear()
                self.prepare_restore()
                self.cog.security_actor = AsyncMock(return_value=member)
                await self.cog.security_member_role_restore(self.before, self.after)
                member.remove_roles.assert_not_awaited()

    async def test_equal_top_role_attempts_only_lower_editable_roles(self):
        equal = self.member(1, [self.staff_role, self.guild.me.top_role, Role(500, 3, managed=True)])
        await self.punish(equal)
        self.assertEqual(equal.remove_roles.call_args.args, (self.staff_role,))

    async def test_no_punishment_for_unrelated_roles_or_unconfirmed_actor(self):
        self.database.get_security_role_removals.return_value = [{'role_id': 555, 'removed_at': self.now}]
        self.cog.find_security_audit_entry = AsyncMock(return_value=None)
        await self.cog.security_member_role_restore(self.before, self.after)
        self.cog.find_security_audit_entry.assert_not_awaited()
        self.prepare_restore()
        self.cog.find_security_audit_entry = AsyncMock(return_value=None)
        await self.cog.security_member_role_restore(self.before, self.after)
        self.actor.remove_roles.assert_not_awaited()

    async def test_duplicate_gateway_events_punish_once(self):
        self.prepare_restore()
        self.cog.find_security_audit_entry = AsyncMock(return_value=self.entry())
        await asyncio.gather(*(self.cog.security_member_role_restore(self.before, self.after) for _ in range(2)))
        self.actor.remove_roles.assert_awaited_once()

    async def test_audit_matching_ignores_removal_wrong_target_and_wrong_added_role(self):
        removal = self.entry(entry_id=1)
        removal.after.roles = []
        removal.before.roles = [self.role]
        wrong_target = self.entry(entry_id=2)
        wrong_target.target.id = 3
        unrelated = self.entry(entry_id=3, roles=[self.staff_role])
        match = self.entry(entry_id=4)
        async def entries(**kwargs):
            for entry in (removal, wrong_target, unrelated, match):
                yield entry
        self.guild.audit_logs = entries
        found = await self.cog.find_security_audit_entry(
            self.guild, disnake.AuditLogAction.member_role_update, 2, added_role_ids={101})
        self.assertIs(found, match)

    async def test_two_people_restoring_different_roles_are_both_punished(self):
        second = Role(103, 1)
        other_actor = self.member(3, [self.staff_role])
        self.after.roles.append(second)
        self.prepare_restore()
        self.database.get_security_role_removals.return_value.append({'role_id': 103, 'removed_at': self.now - timedelta(minutes=2)})
        self.cog.find_security_audit_entry = AsyncMock(side_effect=[self.entry(), self.entry(124, 3, [second])])
        self.cog.security_actor = AsyncMock(side_effect=[self.actor, other_actor])
        await self.cog.security_member_role_restore(self.before, self.after)
        self.actor.remove_roles.assert_awaited_once()
        other_actor.remove_roles.assert_awaited_once()

    async def test_missing_manage_roles_does_not_record_sanction(self):
        self.guild.me.guild_permissions.manage_roles = False
        await self.punish()
        self.actor.remove_roles.assert_not_awaited()
        self.database.record_security_role_removals.assert_not_awaited()

    async def test_blacklist_deletion_uses_same_tracking_and_developer_exemption(self):
        self.cog.publish_blacklist_entry = AsyncMock(return_value=True)
        await self.cog.punish_unauthorized_blacklist_deletion(self.actor, [{'id': 1}])
        self.assertEqual(self.database.record_security_role_removals.call_args.args[:3], (GUILD_ID, 1, [102]))
        developer = self.member(DEVELOPER_USER_ID, [self.staff_role])
        await self.cog.punish_unauthorized_blacklist_deletion(developer, [{'id': 1}])
        developer.remove_roles.assert_not_awaited()

    async def test_developer_bot_invite_does_not_require_guild_member_or_roles(self):
        for bot_id in (2, 3):
            with self.subTest(bot_id=bot_id):
                invited = self.member(bot_id, [])
                invited.bot = True
                invited.ban = AsyncMock()
                entry = self.entry(actor_id=DEVELOPER_USER_ID)
                entry.target.id = bot_id
                self.cog.find_security_audit_entry = AsyncMock(return_value=entry)

                await self.cog.security_bot_join(invited)

                self.cog.find_security_audit_entry.assert_awaited_once_with(
                    self.guild, disnake.AuditLogAction.bot_add, bot_id,
                )
                invited.ban.assert_not_awaited()
                self.guild.fetch_member.assert_not_awaited()
                self.database.record_security_role_removals.assert_not_awaited()
                self.assertIn('6-й уровень', self.log.call_args.kwargs['description'])

    async def test_unauthorized_bot_invite_still_bans_bot_and_punishes_actor(self):
        invited = self.member(2, [])
        invited.bot = True
        invited.ban = AsyncMock()
        self.cog.find_security_audit_entry = AsyncMock(return_value=self.entry())

        await self.cog.security_bot_join(invited)

        invited.ban.assert_awaited_once()
        self.actor.remove_roles.assert_awaited_once()

    async def test_bot_invite_with_unknown_actor_still_bans_bot(self):
        invited = self.member(2, [])
        invited.bot = True
        invited.ban = AsyncMock()
        self.cog.find_security_audit_entry = AsyncMock(return_value=None)

        await self.cog.security_bot_join(invited)

        invited.ban.assert_awaited_once()
        self.assertIn('не найден пригласивший', self.log.call_args.kwargs['title'])

    async def test_logs_reach_both_channels_even_if_general_log_is_forbidden(self):
        general = Mock(spec=disnake.TextChannel)
        general.send = AsyncMock(side_effect=disnake.DiscordException('denied'))
        security = Mock(spec=disnake.TextChannel)
        security.send = AsyncMock()
        bot = SimpleNamespace(get_channel=lambda cid: {ADMIN_LOG_CHANNEL_ID: general, SECURITY_LOG_CHANNEL_ID: security}[cid])
        with self.assertLogs(level='ERROR'):
            await send_security_log(bot, title='security', description='roles removed')
        general.send.assert_awaited_once()
        security.send.assert_awaited_once()
        self.assertEqual(general.send.call_args.kwargs['embed'].to_dict()['description'],
                         security.send.call_args.kwargs['embed'].to_dict()['description'])

    def log_protection(self):
        from cogs.log_protection import LogProtectionCog
        self.bot.user = SimpleNamespace(id=900)
        self.bot.get_cog = Mock(return_value=self.cog)
        self.bot.get_guild = Mock(return_value=self.guild)
        self.bot.get_channel = Mock(return_value=None)
        self.database.record_protected_log_messages = AsyncMock()
        self.database.get_protected_log_message_ids = AsyncMock(return_value=set())
        self.database.prune_protected_log_messages = AsyncMock()
        protection = LogProtectionCog(self.bot)
        protection.started_at = self.now - timedelta(seconds=1)
        return protection

    def log_deletion_entry(self, entry_id=456, *, actor_id=1, count=1, bulk=False):
        return SimpleNamespace(
            id=entry_id, guild=self.guild, user=SimpleNamespace(id=actor_id),
            target=SimpleNamespace(id=ADMIN_LOG_CHANNEL_ID if bulk else 900),
            extra=SimpleNamespace(channel=SimpleNamespace(id=ADMIN_LOG_CHANNEL_ID), count=count),
            action=disnake.AuditLogAction.message_bulk_delete if bulk else disnake.AuditLogAction.message_delete,
            created_at=self.now,
        )

    async def test_log_deletion_by_administrator_is_punished_and_recorded(self):
        protection = self.log_protection()
        self.actor.guild_permissions.administrator = True
        await protection.log_message_delete_audit(self.log_deletion_entry())
        self.actor.remove_roles.assert_awaited_once()
        self.database.record_security_role_removals.assert_awaited_once()
        self.assertIn('удаление сообщений бота из журнала', self.log.call_args.kwargs['title'])

    async def test_log_deletion_ignores_other_authors_channels_guilds_and_self(self):
        protection = self.log_protection()
        for change in ('author', 'channel', 'guild', 'self'):
            with self.subTest(change=change):
                entry = self.log_deletion_entry(entry_id=1000 + len(change))
                if change == 'author':
                    entry.target.id = 77
                elif change == 'channel':
                    entry.extra.channel.id = 88
                elif change == 'guild':
                    entry.guild = SimpleNamespace(id=99)
                else:
                    entry.user.id = 900
                await protection.log_message_delete_audit(entry)
        self.actor.remove_roles.assert_not_awaited()

    async def test_log_deletion_developer_and_higher_role_exemptions(self):
        protection = self.log_protection()
        for index, member in enumerate((self.member(DEVELOPER_USER_ID, [self.staff_role]),
                                       self.member(1, [Role(700, 11)]))):
            self.cog.security_actor = AsyncMock(return_value=member)
            await protection.log_message_delete_audit(self.log_deletion_entry(entry_id=2000 + index))
            member.remove_roles.assert_not_awaited()

    async def test_log_deletion_deduplicates_and_detects_audit_counter_increase(self):
        protection = self.log_protection()
        entry = self.log_deletion_entry()
        await asyncio.gather(*(protection.log_message_delete_audit(entry) for _ in range(2)))
        self.assertEqual(self.actor.remove_roles.await_count, 1)
        entry.extra.count = 2
        await protection.log_message_delete_audit(entry)
        self.assertEqual(self.actor.remove_roles.await_count, 2)
        await protection.log_message_delete_audit(self.log_deletion_entry(count=1))
        self.assertEqual(self.actor.remove_roles.await_count, 2)

    async def test_stale_audit_does_not_punish_on_a_new_unaudited_deletion(self):
        protection = self.log_protection()
        entry = self.log_deletion_entry()
        entry.created_at = self.now - timedelta(minutes=1)
        async def audit(**kwargs):
            yield entry
        self.guild.audit_logs = audit
        payload = SimpleNamespace(guild_id=GUILD_ID, channel_id=ADMIN_LOG_CHANNEL_ID, cached_message=None)
        with patch('cogs.log_protection.asyncio.sleep', AsyncMock()):
            await protection.log_message_delete(payload)
        self.actor.remove_roles.assert_not_awaited()
        entry.extra.count = 2
        await protection.log_message_delete_audit(entry)
        self.actor.remove_roles.assert_awaited_once()

    async def test_bulk_deletion_uses_persisted_bot_message_ids_without_cache(self):
        protection = self.log_protection()
        self.database.get_protected_log_message_ids.return_value = {42}
        entry = self.log_deletion_entry(count=2, bulk=True)
        async def audit(**kwargs):
            yield entry
        self.guild.audit_logs = audit
        payload = SimpleNamespace(guild_id=GUILD_ID, channel_id=ADMIN_LOG_CHANNEL_ID,
                                  message_ids={42, 43}, cached_messages=[])
        await protection.log_bulk_message_delete(payload)
        self.actor.remove_roles.assert_awaited_once()
        self.assertIn('**1**', self.log.call_args.kwargs['description'])
        await protection.log_bulk_message_delete(payload)
        self.actor.remove_roles.assert_awaited_once()

    async def test_bulk_deletion_only_human_messages_is_not_punished(self):
        protection = self.log_protection()
        payload = SimpleNamespace(guild_id=GUILD_ID, channel_id=ADMIN_LOG_CHANNEL_ID,
                                  message_ids={42}, cached_messages=[SimpleNamespace(id=42, author=SimpleNamespace(id=1))])
        await protection.log_bulk_message_delete(payload)
        self.actor.remove_roles.assert_not_awaited()

    async def test_bulk_deletion_ambiguous_actors_are_not_guessed(self):
        protection = self.log_protection()
        payload = SimpleNamespace(guild_id=GUILD_ID, channel_id=ADMIN_LOG_CHANNEL_ID,
                                  message_ids={42, 43}, cached_messages=[SimpleNamespace(id=42, author=SimpleNamespace(id=900))])
        async def audit(**kwargs):
            yield self.log_deletion_entry(count=2, bulk=True)
            yield self.log_deletion_entry(entry_id=457, actor_id=3, count=2, bulk=True)
        self.guild.audit_logs = audit
        with patch('cogs.log_protection.send_security_log', AsyncMock()) as warning:
            await protection.log_bulk_message_delete(payload)
        self.actor.remove_roles.assert_not_awaited()
        warning.assert_awaited_once()

    async def test_bot_log_messages_are_indexed_but_human_messages_are_not(self):
        protection = self.log_protection()
        message = SimpleNamespace(id=42, guild=self.guild, channel=SimpleNamespace(id=ADMIN_LOG_CHANNEL_ID),
                                  author=SimpleNamespace(id=900))
        await protection.remember_log_message(message)
        self.database.record_protected_log_messages.assert_awaited_once_with(GUILD_ID, ADMIN_LOG_CHANNEL_ID, [42])
        message.author.id = 1
        await protection.remember_log_message(message)
        self.database.record_protected_log_messages.assert_awaited_once()

    async def test_all_configured_log_channels_are_protected(self):
        import config
        protection = self.log_protection()
        for key, value in vars(config).items():
            if key.endswith('_LOG_CHANNEL_ID'):
                self.assertTrue(protection.is_log_channel(value), key)
        self.assertTrue(protection.is_log_channel(config.FORWARD_CHANNEL_ID))
        self.assertTrue(protection.is_log_channel(config.CHAT_ERROR_CHANNEL_ID))

    async def test_bulk_waits_for_delayed_second_audit_before_attributing_actor(self):
        protection = self.log_protection()
        payload = SimpleNamespace(guild_id=GUILD_ID, channel_id=ADMIN_LOG_CHANNEL_ID,
                                  message_ids={42, 43}, cached_messages=[SimpleNamespace(id=42, author=SimpleNamespace(id=900))])
        calls = 0
        async def audit(**kwargs):
            nonlocal calls
            calls += 1
            yield self.log_deletion_entry(count=2, bulk=True)
            if calls > 1:
                yield self.log_deletion_entry(entry_id=457, actor_id=3, count=2, bulk=True)
        self.guild.audit_logs = audit
        with patch('cogs.log_protection.asyncio.sleep', AsyncMock()), patch('cogs.log_protection.send_security_log', AsyncMock()):
            await protection.log_bulk_message_delete(payload)
        self.actor.remove_roles.assert_not_awaited()
        self.assertGreater(calls, 1)

    async def test_log_history_backfill_indexes_older_bot_messages_and_seeds_audit(self):
        protection = self.log_protection()
        self.guild.threads = []
        channel = Mock(spec=disnake.TextChannel)
        channel.id = ADMIN_LOG_CHANNEL_ID
        self.guild.get_channel = lambda cid: channel if cid == ADMIN_LOG_CHANNEL_ID else None
        async def history(**kwargs):
            yield SimpleNamespace(id=42, author=SimpleNamespace(id=900))
            yield SimpleNamespace(id=43, author=SimpleNamespace(id=1))
        channel.history = history
        entry = self.log_deletion_entry()
        entry.created_at = self.now - timedelta(minutes=1)
        async def audit(**kwargs):
            yield entry
        self.guild.audit_logs = audit
        await protection.on_ready()
        self.database.prune_protected_log_messages.assert_awaited_once()
        self.database.record_protected_log_messages.assert_awaited_once_with(GUILD_ID, ADMIN_LOG_CHANNEL_ID, [42])
        await protection.log_message_delete_audit(entry)
        self.actor.remove_roles.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
