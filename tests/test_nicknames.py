import asyncio
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import disnake

from cogs.nicknames import NicknamesCog, nickname_format_kind, valid_nickname
from config import DEVELOPER_USER_ID, FAMILY_MEMBER_ROLE_ID, FAMILY_RANK_ROLE_IDS, GUILD_ID, MEDIA_ROLE_ID, NICKNAME_LOG_CHANNEL_ID


class NicknameFormatTests(unittest.TestCase):
    def test_member_requires_first_name_pipe_optional_real_name_and_static(self):
        for nickname in ('Alex | 123', 'Alex | Иван | #123', 'Ник|123',
                         ' Jean-Luc | Анна-Мария | 0012 ', "D'Angelo | 123"):
            with self.subTest(nickname=nickname):
                self.assertTrue(valid_nickname(nickname, 'member', {'Seattle'}))
        for nickname in ('Nick', 'Nick / Name', '123 / 456', '/Nick/123', 'Nick/123/',
                         'Nick/Name/Other/123', 'Nick/abc123', 'Nick\n|123', 'Nick\u200b|123',
                         'Nick_Name | 123', 'Nick Name | Иван | 123', 'Nick/123',
                         'Nick \\ Name \\ 123', 'Nick || 123', 'Nick - Иван - 123',
                         'Nick • 123', 'Seattle | Nick | 123', 'Nick | Seattle | 123',
                         'Nick | Иван | Seattle | 123', 'Nick | Иван | 123 Seattle',
                         'Nick | Иван |', '|123', 'Nick | 123 |', 'Nick | abc123'):
            with self.subTest(nickname=nickname):
                self.assertFalse(valid_nickname(nickname, 'member', {'Seattle'}))

    def test_deputy_leader_format(self):
        for nickname in ('Nick_Name', 'Nick Name', 'Nick | Иван', 'Nick/Иван'):
            self.assertTrue(valid_nickname(nickname, 'deputy leader', set()))
        for nickname in ('123', 'Nick/123', 'Nick/Name/123', 'Nick/'):
            self.assertFalse(valid_nickname(nickname, 'deputy leader', set()))

    def test_leader_requires_configured_server(self):
        for nickname in ('Seattle / Nick', ' seATTLE | Nick | Иван ', 'Denver - Nick'):
            self.assertTrue(valid_nickname(nickname, 'leader', {'Seattle', 'Denver'}))
        for nickname in ('Nick', 'Unknown/Nick', 'Seattle/123', 'Seattle/Nick/123'):
            self.assertFalse(valid_nickname(nickname, 'leader', {'Seattle'}))

    def test_rank_specific_formats_and_exclusions(self):
        for rank, role_id in FAMILY_RANK_ROLE_IDS.items():
            expected = None if rank in {'tech', 'curator', 'owner'} else (
                rank if rank in {'leader', 'deputy leader'} else 'member'
            )
            self.assertEqual(nickname_format_kind({role_id}), expected)


class NicknameEnforcementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = disnake.utils.utcnow()
        self.guild = SimpleNamespace(id=GUILD_ID, owner_id=999)
        self.everyone = self.role(GUILD_ID, 0)
        self.family_role = self.role(FAMILY_RANK_ROLE_IDS['young'], 1)
        self.managed = self.role(300, 2, managed=True)
        self.guild.me = SimpleNamespace(top_role=self.role(900, 20),
                                        guild_permissions=disnake.Permissions(manage_roles=True))
        self.member = SimpleNamespace(
            id=100, guild=self.guild, bot=False, nick='Bad', display_name='Bad',
            roles=[self.everyone, self.family_role, self.managed], top_role=self.managed,
            mention='<@100>', send=AsyncMock(), remove_roles=AsyncMock(),
        )
        self.guild.fetch_member = AsyncMock(return_value=self.member)
        self.guild.get_member = Mock(return_value=self.member)
        self.guild.members = [self.member]
        self.warnings = {}

        async def save(guild_id, member_id, nickname, deadline):
            key = (guild_id, member_id)
            row = self.warnings.setdefault(key, dict(nickname=nickname, deadline=deadline, penalized_at=None))
            row['nickname'] = nickname
            return row.copy()

        async def get(guild_id, member_id):
            row = self.warnings.get((guild_id, member_id))
            return row.copy() if row is not None else None

        async def clear(guild_id, member_id):
            self.warnings.pop((guild_id, member_id), None)

        async def penalize(guild_id, member_id):
            self.warnings[(guild_id, member_id)]['penalized_at'] = self.now

        self.db = SimpleNamespace(
            get_families=AsyncMock(return_value=[]), get_mafia_saved_access=AsyncMock(return_value=0),
            get_game_servers=AsyncMock(return_value=[{'name': 'Seattle'}]),
            get_nickname_warning=AsyncMock(side_effect=get), save_nickname_warning=AsyncMock(side_effect=save),
            clear_nickname_warning=AsyncMock(side_effect=clear), mark_nickname_penalty=AsyncMock(side_effect=penalize),
            get_dm_notifications_enabled=AsyncMock(return_value=False),
        )
        self.bot = SimpleNamespace(database=self.db, get_guild=Mock(return_value=self.guild))
        with patch.object(NicknamesCog, 'check_nicknames'), patch.object(NicknamesCog, 'check_deadlines'):
            self.cog = NicknamesCog(self.bot)
        self.log = AsyncMock()
        patcher = patch('cogs.nicknames.send_admin_log', self.log)
        patcher.start()
        self.addCleanup(patcher.stop)

    def role(self, role_id, position, managed=False):
        return disnake.Role(guild=self.guild, state=Mock(), data={
            'id': str(role_id), 'name': str(role_id), 'position': position,
            'permissions': '0', 'managed': managed,
            'colors': {'primary_color': 0, 'secondary_color': None, 'tertiary_color': None},
        })

    def expired_warning(self):
        self.warnings[(GUILD_ID, 100)] = dict(
            nickname='Bad', deadline=self.now - timedelta(seconds=1), penalized_at=None,
        )

    async def test_first_warning_bypasses_disabled_notifications_and_gives_24_hours(self):
        with patch('cogs.nicknames.disnake.utils.utcnow', return_value=self.now):
            await self.cog.check_member(self.member)
        self.assertEqual(self.warnings[(GUILD_ID, 100)]['deadline'], self.now + timedelta(hours=24))
        self.member.send.assert_awaited_once()
        message = self.member.send.call_args.args[0]
        self.assertIn('без фамилии', message)
        self.assertIn('Название сервера писать не нужно', message)
        self.assertIn('Alex | 12345', message)
        self.assertNotIn('Вместо `/`', message)
        self.member.remove_roles.assert_not_awaited()
        self.db.get_dm_notifications_enabled.assert_not_awaited()
        self.assertEqual(self.log.call_args.kwargs['channel_id'], NICKNAME_LOG_CHANNEL_ID)

    async def test_daily_rechecks_do_not_repeat_warning_or_extend_deadline(self):
        await self.cog.check_member(self.member)
        deadline = self.warnings[(GUILD_ID, 100)]['deadline']
        await self.cog.check_member(self.member)
        self.assertEqual(self.warnings[(GUILD_ID, 100)]['deadline'], deadline)
        self.member.send.assert_awaited_once()

    async def test_invalid_rename_keeps_deadline_and_valid_rename_clears_it(self):
        await self.cog.check_member(self.member)
        deadline = self.warnings[(GUILD_ID, 100)]['deadline']
        self.member.display_name = 'Still bad'
        await self.cog.check_member(self.member, changed=True)
        self.assertEqual(self.warnings[(GUILD_ID, 100)]['deadline'], deadline)
        self.assertIn('Срок не изменился', self.member.send.call_args.args[0])
        self.member.display_name = 'Nick | 123'
        await self.cog.check_member(self.member, changed=True)
        self.assertFalse(self.warnings)
        self.assertIn('Таймер исправления сброшен', self.member.send.call_args.args[0])

    async def test_expired_warning_removes_only_editable_roles_and_logs_once(self):
        self.expired_warning()
        await self.cog.check_member(self.member)
        self.assertEqual(self.member.remove_roles.call_args.args, (self.family_role,))
        self.guild.fetch_member.assert_awaited_once_with(100)
        self.assertTrue(any('Роли сняты' in call.kwargs['title'] for call in self.log.call_args_list))
        await self.cog.check_member(self.member)
        self.member.remove_roles.assert_awaited_once()

    async def test_deadline_loop_enforces_persisted_warning_without_name_event(self):
        self.expired_warning()
        self.db.get_due_nickname_warnings = AsyncMock(return_value=[
            dict(self.warnings[(GUILD_ID, 100)], member_id=100),
        ])
        await NicknamesCog.check_deadlines.coro(self.cog)
        self.member.remove_roles.assert_awaited_once()
        self.guild.fetch_member.assert_awaited_once_with(100)

    async def test_penalty_removes_famq_and_trigger_roles_in_one_update(self):
        famq = self.role(FAMILY_MEMBER_ROLE_ID, 1)
        self.member.roles.insert(1, famq)
        self.expired_warning()
        await self.cog.check_member(self.member)
        self.member.remove_roles.assert_awaited_once_with(
            famq, self.family_role,
            reason="Никнейм не исправлен в течение 24 часов", atomic=False,
        )

    async def test_deadline_is_strict_and_does_not_penalize_early(self):
        self.expired_warning()
        self.warnings[(GUILD_ID, 100)]['deadline'] = self.now
        with patch('cogs.nicknames.disnake.utils.utcnow', return_value=self.now - timedelta(microseconds=1)):
            await self.cog.check_member(self.member)
        self.member.remove_roles.assert_not_awaited()
        with patch('cogs.nicknames.disnake.utils.utcnow', return_value=self.now):
            await self.cog.check_member(self.member)
        self.member.remove_roles.assert_awaited_once()

    async def test_live_corrected_nickname_prevents_penalty_even_with_stale_cache(self):
        self.expired_warning()
        fresh = SimpleNamespace(**vars(self.member))
        fresh.display_name = 'Nick | 123'
        self.guild.fetch_member.return_value = fresh
        await self.cog.check_member(self.member)
        self.member.remove_roles.assert_not_awaited()
        self.assertFalse(self.warnings)

    async def test_failed_dm_does_not_start_unannounced_penalty(self):
        self.member.send.side_effect = disnake.DiscordException('closed DMs')
        with self.assertLogs(level='WARNING'):
            await self.cog.check_member(self.member)
        self.assertFalse(self.warnings)
        self.member.remove_roles.assert_not_awaited()
        self.assertIn('не доставлено', self.log.call_args.kwargs['title'])

    async def test_failed_removal_keeps_timer_for_retry(self):
        self.expired_warning()
        self.member.remove_roles.side_effect = disnake.DiscordException('denied')
        await self.cog.check_member(self.member)
        self.db.mark_nickname_penalty.assert_not_awaited()
        self.assertIn('Ошибка снятия', self.log.call_args.kwargs['title'])

    async def test_developer_exempt_and_other_guild_ignored(self):
        self.member.id = DEVELOPER_USER_ID
        await self.cog.check_member(self.member)
        self.member.send.assert_not_awaited()
        self.member.guild.id = 123
        self.db.get_nickname_warning.reset_mock()
        await self.cog.check_member(self.member)
        self.db.get_nickname_warning.assert_not_awaited()

    async def test_higher_role_and_owner_cannot_be_punished(self):
        self.expired_warning()
        for top_role, owner_id in ((self.guild.me.top_role, 999), (self.managed, 100)):
            self.member.top_role = top_role
            self.guild.owner_id = owner_id
            await self.cog.check_member(self.member)
            self.member.remove_roles.assert_not_awaited()

    async def test_mafia_holds_suspend_name_enforcement(self):
        self.expired_warning()
        self.db.get_mafia_saved_access.return_value = 5
        await self.cog.check_member(self.member)
        self.member.remove_roles.assert_not_awaited()
        self.db.clear_nickname_warning.assert_not_awaited()

    async def test_media_is_not_warned_for_invalid_nickname(self):
        self.member.roles.append(self.role(MEDIA_ROLE_ID, 3))
        await self.cog.check_member(self.member)
        self.member.send.assert_not_awaited()
        self.db.save_nickname_warning.assert_not_awaited()

    async def test_media_on_live_member_cancels_expired_nickname_penalty(self):
        self.expired_warning()
        fresh = SimpleNamespace(**vars(self.member))
        fresh.roles = [*self.member.roles, self.role(MEDIA_ROLE_ID, 3)]
        self.guild.fetch_member.return_value = fresh
        await self.cog.check_member(self.member)
        self.member.remove_roles.assert_not_awaited()
        self.member.send.assert_not_awaited()
        self.assertFalse(self.warnings)

    async def test_removing_media_starts_a_new_nickname_deadline(self):
        media = self.role(MEDIA_ROLE_ID, 3)
        self.member.roles.append(media)
        self.expired_warning()
        await self.cog.check_member(self.member)
        self.member.roles.remove(media)
        with patch('cogs.nicknames.disnake.utils.utcnow', return_value=self.now):
            await self.cog.check_member(self.member)
        self.assertEqual(self.warnings[(GUILD_ID, 100)]['deadline'], self.now + timedelta(hours=24))
        self.member.remove_roles.assert_not_awaited()

    async def test_simultaneous_checks_warn_once(self):
        await asyncio.gather(*(self.cog.check_member(self.member) for _ in range(3)))
        self.member.send.assert_awaited_once()

    async def test_global_name_update_is_checked_only_without_server_nickname(self):
        before = SimpleNamespace(display_name='Old')
        after = SimpleNamespace(id=100, display_name='Bad')
        self.cog.check_member = AsyncMock()
        await self.cog.on_user_update(before, after)
        self.cog.check_member.assert_not_awaited()
        self.member.nick = None
        await self.cog.on_user_update(before, after)
        self.cog.check_member.assert_awaited_once_with(self.member, changed=True)

    async def test_duplicate_name_events_send_one_success(self):
        self.member.display_name = 'Nick | 123'
        await self.cog.check_member(self.member, changed=True)
        await self.cog.check_member(self.member, changed=True)
        self.member.send.assert_awaited_once()
