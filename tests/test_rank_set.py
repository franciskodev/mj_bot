from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import disnake

from access_control import command_access_level
from cogs.systems import SystemsCog
from config import DEVELOPER_USER_ID, FAMILY_RANK_ROLE_IDS, GUILD_ID


class RankSetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=GUILD_ID, owner_id=999)
        self.roles = {rank: self.role(role_id, index + 1)
                      for index, (rank, role_id) in enumerate(FAMILY_RANK_ROLE_IDS.items())}
        self.everyone = self.role(GUILD_ID, 0)
        self.other = self.role(500, 1)
        self.bot_role = self.role(900, 20)
        self.guild.me = SimpleNamespace(top_role=self.bot_role,
                                       guild_permissions=disnake.Permissions(manage_roles=True))
        self.guild.get_role = lambda rid: next((role for role in self.roles.values() if role.id == rid), None)
        self.reviewer = self.member(10, 'deputy leader')
        self.target = self.member(20, 'young')
        self.target.roles.append(self.other)
        self.guild.fetch_member = AsyncMock(side_effect=lambda mid: self.reviewer if mid == self.reviewer.id else self.target)
        self.db = SimpleNamespace(get_mafia_saved_access=AsyncMock(return_value=0))
        self.bot = SimpleNamespace(database=self.db)
        self.cog = SystemsCog(self.bot)
        self.inter = SimpleNamespace(
            guild=self.guild, guild_id=GUILD_ID, author=self.reviewer,
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
        )
        self.log = AsyncMock()
        for patcher in (patch('cogs.systems.send_admin_log', self.log),
                        patch('access_control.member_access_level', AsyncMock(return_value=4))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def role(self, role_id, position):
        return disnake.Role(guild=self.guild, state=Mock(), data={
            'id': str(role_id), 'name': str(role_id), 'position': position, 'permissions': '0',
            'colors': {'primary_color': 0, 'secondary_color': None, 'tertiary_color': None},
        })

    def member(self, member_id, rank):
        member = Mock(spec=disnake.Member)
        member.id, member.guild = member_id, self.guild
        member.roles = [self.everyone, self.roles[rank]]
        member.top_role = self.roles[rank]
        member.mention = f'<@{member_id}>'
        member.edit = AsyncMock()
        return member

    async def invoke(self, rank='old'):
        await SystemsCog.rank_set.callback(self.cog, self.inter, self.target, rank)

    async def test_rank_replaces_old_rank_and_preserves_other_roles(self):
        await self.invoke()
        self.assertEqual(self.target.edit.call_args.kwargs['roles'], [self.other, self.roles['old']])
        self.log.assert_awaited_once()
        self.assertEqual(await command_access_level(self.bot, GUILD_ID, 'rank_set'), 4)

    async def test_lowering_rank_is_allowed_for_subordinate(self):
        self.target.roles.append(self.roles['old'])
        self.target.top_role = self.roles['old']
        await self.invoke('main')
        self.assertEqual(self.target.edit.call_args.kwargs['roles'], [self.other, self.roles['main']])

    async def test_own_and_higher_ranks_are_rejected(self):
        for rank in ('deputy leader', 'tech', 'leader', 'curator', 'owner'):
            with self.subTest(rank=rank):
                await self.invoke(rank)
                self.target.edit.assert_not_awaited()

    async def test_cannot_demote_peer_even_if_discord_role_is_lower(self):
        self.target.roles.append(self.roles['deputy leader'])
        await self.invoke('young')
        self.target.edit.assert_not_awaited()

    async def test_developer_target_and_self_are_protected(self):
        self.target.id = DEVELOPER_USER_ID
        await self.invoke()
        self.target.edit.assert_not_awaited()
        self.target = self.reviewer
        await self.invoke()
        self.target.edit.assert_not_awaited()

    async def test_level_three_is_rejected(self):
        with patch('access_control.member_access_level', AsyncMock(return_value=3)):
            await self.invoke()
        self.target.edit.assert_not_awaited()

    async def test_mafia_held_roles_prevent_changes(self):
        self.db.get_mafia_saved_access.return_value = 5
        await self.invoke()
        self.target.edit.assert_not_awaited()

    async def test_missing_bot_permission_and_managed_rank_prevent_changes(self):
        self.guild.me.guild_permissions.manage_roles = False
        await self.invoke()
        self.target.edit.assert_not_awaited()
        self.guild.me.guild_permissions.manage_roles = True
        self.roles['old'].managed = True
        await self.invoke()
        self.target.edit.assert_not_awaited()

    async def test_api_failure_reports_no_success(self):
        self.target.edit.side_effect = disnake.DiscordException('denied')
        with self.assertLogs(level='ERROR'):
            await self.invoke()
        self.log.assert_not_awaited()
        self.assertIn('Не удалось', self.inter.edit_original_response.call_args.kwargs['content'])
