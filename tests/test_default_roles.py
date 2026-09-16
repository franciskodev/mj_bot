from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import disnake

from cogs.core import CoreCog
from config import DEFAULT_MEMBER_ROLE_ID, GUILD_ID


class DefaultRoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.role = SimpleNamespace(id=DEFAULT_MEMBER_ROLE_ID)
        self.guild = SimpleNamespace(
            id=GUILD_ID, chunked=True, members=[], chunk=AsyncMock(),
            get_role=Mock(return_value=self.role), fetch_member=AsyncMock(),
        )
        self.bot = SimpleNamespace(get_guild=Mock(return_value=self.guild))
        with patch.object(CoreCog, 'sync_default_roles'):
            self.cog = CoreCog(self.bot)

    def member(self, member_id, *, roles=None, bot=False):
        return SimpleNamespace(
            id=member_id, guild=self.guild, bot=bot,
            roles=roles if roles is not None else [SimpleNamespace(id=GUILD_ID)],
            add_roles=AsyncMock(),
        )

    async def test_daily_scan_loads_all_members_and_assigns_join_role(self):
        roleless = self.member(1)
        ranked = self.member(2, roles=[GUILD_ID, 42])
        newbie = self.member(3, roles=[GUILD_ID, DEFAULT_MEMBER_ROLE_ID])
        bot = self.member(4, bot=True)
        self.guild.chunked = False

        async def populate():
            self.guild.members = [roleless, ranked, newbie, bot]

        self.guild.chunk.side_effect = populate
        self.guild.fetch_member.return_value = roleless
        await CoreCog.sync_default_roles.coro(self.cog)
        self.guild.chunk.assert_awaited_once()
        roleless.add_roles.assert_awaited_once_with(
            self.role, reason="Ежедневная проверка участников без ролей",
        )
        for member in (ranked, newbie, bot):
            member.add_roles.assert_not_awaited()
        self.bot.get_guild.assert_called_once_with(GUILD_ID)
        self.assertEqual(CoreCog.sync_default_roles.hours, 24)

    async def test_new_roles_since_cached_snapshot_prevent_newbie(self):
        cached = self.member(1)
        live = self.member(1, roles=[GUILD_ID, 42])
        self.guild.members = [cached]
        self.guild.fetch_member.return_value = live
        await CoreCog.sync_default_roles.coro(self.cog)
        cached.add_roles.assert_not_awaited()
        live.add_roles.assert_not_awaited()

    async def test_member_api_failure_does_not_skip_remaining_members(self):
        failed, remaining = self.member(1), self.member(2)
        failed.add_roles.side_effect = disnake.DiscordException('denied')
        self.guild.members = [failed, remaining]
        self.guild.fetch_member.side_effect = [failed, remaining]
        with self.assertLogs(level='ERROR'):
            await CoreCog.sync_default_roles.coro(self.cog)
        remaining.add_roles.assert_awaited_once()

    async def test_failed_member_loading_does_not_stop_loop_with_exception(self):
        self.guild.chunked = False
        self.guild.chunk.side_effect = TimeoutError()
        with self.assertLogs(level='ERROR'):
            await CoreCog.sync_default_roles.coro(self.cog)
        self.guild.fetch_member.assert_not_awaited()
