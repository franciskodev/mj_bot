import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import disnake

from cogs.invite_filter import InviteFilterCog, discord_links
from cogs.systems import SystemsCog
from config import DEVELOPER_USER_ID, GUILD_ID
from test_security import Role


class LinkParsingTests(unittest.TestCase):
    def test_invite_formats_and_obfuscation(self):
        for content in (
            'https://discord.gg/Ab-c_123', 'discord.gg/Ab-c_123',
            '[join](https://www.discord.com/invite/Ab-c_123?event=1)',
            '<HTTP://PTB.DISCORDAPP.COM/INVITE/Ab-c_123>',
            'https://canary.discord.com/invite/Ab-c_123',
            'discord\u200b.gg/Ab-c_123', 'https://discord%2Egg/Ab-c_123',
        ):
            with self.subTest(content=content):
                self.assertEqual(discord_links(content), ({'Ab-c_123'}, set()))

    def test_unrelated_urls_and_similar_domains(self):
        for content in ('https://discord.com/developers', 'https://discord.com/channels/@me/12',
                        'https://notdiscord.gg/test', 'https://fake.discord.gg/test',
                        'https://discord.gg.example.org/test', 'ordinary text'):
            self.assertEqual(discord_links(content), (set(), set()))

    def test_direct_channel_links(self):
        self.assertEqual(discord_links('https://discord.com/channels/123/456/789'), (set(), {123}))


class InviteFilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=GUILD_ID, owner_id=999)
        self.everyone = Role(GUILD_ID, 0)
        self.role = Role(101, 1)
        self.bot_role = Role(900, 10)
        self.guild.me = SimpleNamespace(
            id=900, top_role=self.bot_role,
            guild_permissions=disnake.Permissions(manage_roles=True),
        )
        self.member = Mock(spec=disnake.Member)
        self.member.id = 1
        self.member.guild = self.guild
        self.member.bot = False
        self.member.top_role = self.role
        self.member.roles = [self.everyone, self.role]
        self.member.mention = '<@1>'
        self.member.remove_roles = AsyncMock()
        self.member.send = AsyncMock()
        self.channel = SimpleNamespace(id=20, send=AsyncMock(), fetch_message=AsyncMock())
        self.message = SimpleNamespace(
            id=30, guild=self.guild, author=self.member, webhook_id=None,
            content='https://discord.gg/external', channel=self.channel, delete=AsyncMock(),
        )
        self.database = SimpleNamespace(
            record_discord_link_violation=AsyncMock(return_value=False),
            record_security_role_removals=AsyncMock(),
        )
        self.bot = SimpleNamespace(
            database=self.database,
            fetch_invite=AsyncMock(return_value=SimpleNamespace(guild=SimpleNamespace(id=123))),
            get_channel=Mock(return_value=self.channel),
        )
        self.systems = SystemsCog(self.bot)
        self.bot.get_cog = Mock(return_value=self.systems)
        self.cog = InviteFilterCog(self.bot)
        self.access = AsyncMock(return_value=1)
        self.log = AsyncMock()
        for name, replacement in (
            ('cogs.invite_filter.member_access_level', self.access),
            ('cogs.invite_filter.send_security_log', self.log),
            ('cogs.systems.send_security_log', self.log),
        ):
            patcher = patch(name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_first_link_deleted_and_warned_without_removing_roles(self):
        await self.cog.on_message(self.message)
        self.message.delete.assert_awaited_once()
        self.channel.send.assert_awaited_once()
        self.assertIn('12 часов', self.channel.send.call_args.args[0])
        self.member.remove_roles.assert_not_awaited()
        args = self.database.record_discord_link_violation.call_args.args
        self.assertEqual((args[3] - args[4]).total_seconds(), 12 * 3600)

    async def test_repeat_removes_only_manageable_roles_and_records_security_timer(self):
        self.database.record_discord_link_violation.return_value = True
        managed = Role(102, 2, managed=True)
        self.member.roles.extend([managed, self.bot_role])
        self.member.top_role = self.bot_role
        await self.cog.on_message(self.message)
        self.assertEqual(self.member.remove_roles.call_args.args, (self.role,))
        self.database.record_security_role_removals.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_exempt_members_and_current_bot(self):
        for member_id, top_role, level in (
            (DEVELOPER_USER_ID, self.role, 6), (1, self.role, 6),
            (1, Role(1000, 11), 1), (999, self.role, 5), (900, self.bot_role, 0),
        ):
            with self.subTest(member_id=member_id, level=level):
                self.member.id = member_id
                self.member.top_role = top_role
                self.access.return_value = level
                await self.cog.on_message(self.message)
        self.message.delete.assert_not_awaited()
        self.database.record_discord_link_violation.assert_not_awaited()

    async def test_administrator_below_bot_is_not_exempt(self):
        self.access.return_value = 5
        await self.cog.on_message(self.message)
        self.message.delete.assert_awaited_once()

    async def test_dm_and_other_guild_ignored(self):
        for guild in (None, SimpleNamespace(id=1522843375840268338)):
            self.message.guild = guild
            await self.cog.on_message(self.message)
        self.message.delete.assert_not_awaited()

    async def test_own_invites_and_channels_allowed(self):
        self.bot.fetch_invite.return_value.guild.id = GUILD_ID
        self.message.content += f' https://discord.com/channels/{GUILD_ID}/123'
        await self.cog.on_message(self.message)
        self.message.delete.assert_not_awaited()

    async def test_mixed_own_and_external_invites(self):
        self.message.content = 'discord.gg/aaa discord.com/invite/bbb'
        self.bot.fetch_invite.side_effect = [
            SimpleNamespace(guild=SimpleNamespace(id=GUILD_ID)),
            SimpleNamespace(guild=SimpleNamespace(id=123)),
        ]
        await self.cog.on_message(self.message)
        self.message.delete.assert_awaited_once()

    async def test_external_channel_link(self):
        self.message.content = 'https://discord.com/channels/123/456'
        await self.cog.on_message(self.message)
        self.message.delete.assert_awaited_once()
        self.bot.fetch_invite.assert_not_awaited()

    async def test_unverifiable_invite_deleted_without_penalty(self):
        self.bot.fetch_invite.side_effect = disnake.DiscordException('unavailable')
        await self.cog.on_message(self.message)
        self.message.delete.assert_awaited_once()
        self.database.record_discord_link_violation.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()

    async def test_deletion_failure_does_not_disable_warning(self):
        self.message.delete.side_effect = disnake.DiscordException('forbidden')
        with self.assertLogs(level='ERROR'):
            await self.cog.on_message(self.message)
        self.channel.send.assert_awaited_once()

    async def test_warning_falls_back_to_dm(self):
        self.channel.send.side_effect = disnake.DiscordException('forbidden')
        await self.cog.on_message(self.message)
        self.member.send.assert_awaited_once()

    async def test_database_failure_does_not_remove_roles(self):
        self.database.record_discord_link_violation.side_effect = OSError('offline')
        with self.assertLogs(level='ERROR'):
            await self.cog.on_message(self.message)
        self.message.delete.assert_awaited_once()
        self.member.remove_roles.assert_not_awaited()

    async def test_duplicate_events_do_not_escalate(self):
        self.database.record_discord_link_violation.side_effect = [False, None]
        await asyncio.gather(*(self.cog.on_message(self.message) for _ in range(2)))
        self.channel.send.assert_awaited_once()
        self.member.remove_roles.assert_not_awaited()

    async def test_uncached_edit_checked(self):
        self.channel.fetch_message.return_value = self.message
        await self.cog.on_raw_message_edit(SimpleNamespace(
            guild_id=GUILD_ID, channel_id=20, message_id=30,
            data={'content': self.message.content},
        ))
        self.channel.fetch_message.assert_awaited_once_with(30)
        self.message.delete.assert_awaited_once()

    async def test_embed_only_edit_ignored(self):
        await self.cog.on_raw_message_edit(SimpleNamespace(guild_id=GUILD_ID, data={'embeds': []}))
        self.channel.fetch_message.assert_not_awaited()
