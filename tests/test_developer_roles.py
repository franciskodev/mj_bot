import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import disnake

from bot import InterpolovBot
from cogs.systems import SystemsCog
from config import BROADCAST_GUILD_ID, DEVELOPER_USER_ID, GUILD_ID


class DeveloperRoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(database=SimpleNamespace(
            get_command_access_level=AsyncMock(return_value=1),
        ))
        self.cog = SystemsCog(self.bot)
        self.guild = SimpleNamespace(id=GUILD_ID, me=SimpleNamespace(
            top_role=10, guild_permissions=disnake.Permissions(manage_roles=True),
        ))
        self.author = Mock(spec=disnake.Member)
        self.author.id = DEVELOPER_USER_ID
        self.author.mention = f'<@{DEVELOPER_USER_ID}>'
        self.author.guild_permissions = disnake.Permissions.none()
        self.member = SimpleNamespace(id=100, mention='<@100>', roles=[], add_roles=AsyncMock())
        self.role = self.make_role(5)
        self.inter = SimpleNamespace(
            guild=self.guild, guild_id=GUILD_ID, author=self.author,
            data=SimpleNamespace(name='role_give', options=[]),
            response=SimpleNamespace(
                send_message=AsyncMock(), defer=AsyncMock(), is_done=Mock(return_value=False),
            ),
            edit_original_response=AsyncMock(),
        )
        self.log = AsyncMock()
        patcher = patch('cogs.systems.send_admin_log', self.log)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_role(self, position, *, managed=False, default=False):
        role = Mock(spec=disnake.Role)
        role.id = 200
        role.mention = '<@&200>'
        role.managed = managed
        role.is_default.return_value = default
        role.__lt__ = Mock(side_effect=lambda other: position < other)
        role.permissions = disnake.Permissions(administrator=True)
        return role

    async def invoke(self):
        if await InterpolovBot.check_command_access(self.bot, self.inter):
            await SystemsCog.role_give.callback(self.cog, self.inter, self.member, self.role)

    async def test_developer_without_admin_can_grant_admin_role(self):
        await self.invoke()
        self.member.add_roles.assert_awaited_once()
        self.assertEqual(self.member.add_roles.call_args.args, (self.role,))
        self.assertIn(str(DEVELOPER_USER_ID), self.member.add_roles.call_args.kwargs['reason'])
        self.inter.response.defer.assert_awaited_once_with(ephemeral=True)
        self.log.assert_awaited_once()

    async def test_developer_can_grant_role_to_self(self):
        self.member = self.author
        self.member.roles = []
        self.member.add_roles = AsyncMock()
        await self.invoke()
        self.member.add_roles.assert_awaited_once()

    async def test_admin_cannot_use_command_even_with_configured_level_one(self):
        self.author.id = 101
        self.author.guild_permissions.administrator = True
        await self.invoke()
        self.member.add_roles.assert_not_awaited()
        self.bot.database.get_command_access_level.assert_not_awaited()
        self.assertIn('6 — Разработчик', self.inter.response.send_message.call_args.args[0])

    async def test_command_is_restricted_to_main_guild(self):
        for guild_id in (None, BROADCAST_GUILD_ID, 123):
            with self.subTest(guild_id=guild_id):
                self.inter.guild_id = guild_id
                await self.invoke()
                self.member.add_roles.assert_not_awaited()

    async def test_timer_reset_requires_developer_and_main_guild(self):
        self.inter.data.name = 'role_timer_reset'
        for author_id, guild_id, allowed in (
            (DEVELOPER_USER_ID, GUILD_ID, True), (101, GUILD_ID, False),
            (DEVELOPER_USER_ID, BROADCAST_GUILD_ID, False),
            (DEVELOPER_USER_ID, None, False),
        ):
            with self.subTest(author_id=author_id, guild_id=guild_id):
                self.author.id = author_id
                self.author.guild_permissions.administrator = author_id != DEVELOPER_USER_ID
                self.inter.guild_id = guild_id
                self.assertEqual(await InterpolovBot.check_command_access(self.bot, self.inter), allowed)
        self.bot.database.get_command_access_level.assert_not_awaited()

    async def test_equal_higher_managed_and_default_roles_are_rejected(self):
        for role in (self.make_role(10), self.make_role(11),
                     self.make_role(5, managed=True), self.make_role(0, default=True)):
            with self.subTest(role=role):
                self.role = role
                self.inter.response.send_message.reset_mock()
                await self.invoke()
                self.member.add_roles.assert_not_awaited()
                self.inter.response.send_message.assert_awaited_once()
                self.log.assert_not_awaited()

    async def test_missing_bot_permissions_or_member_prevents_grant(self):
        self.guild.me.guild_permissions.manage_roles = False
        await self.invoke()
        self.member.add_roles.assert_not_awaited()
        self.guild.me = None
        await self.invoke()
        self.member.add_roles.assert_not_awaited()

    async def test_existing_role_is_reported_without_grant(self):
        self.member.roles = [self.role]
        await self.invoke()
        self.member.add_roles.assert_not_awaited()
        self.assertIn('уже есть', self.inter.response.send_message.call_args.args[0])

    async def test_discord_failure_is_reported_without_success_log(self):
        self.member.add_roles.side_effect = disnake.DiscordException('denied')
        with self.assertLogs(level='ERROR'):
            await self.invoke()
        self.assertIn('Не удалось', self.inter.edit_original_response.call_args.kwargs['content'])
        self.log.assert_not_awaited()
