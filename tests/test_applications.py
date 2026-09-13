import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import disnake

from access_control import can_review_application, command_access_level
from cogs.applications import ApplicationsCog, application_panel, server_select
from config import DEVELOPER_USER_ID, GUILD_ID
from log_utils import application_log_channel, send_admin_log


class ApplicationAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=GUILD_ID, owner_id=999)
        self.roles = {}
        for role_id in (GUILD_ID, 10, 11, 20, 21, 30, 40):
            permissions = disnake.Permissions(view_channel=True, send_messages=True)
            if role_id == 40:
                permissions.administrator = True
            self.roles[role_id] = disnake.Role(guild=self.guild, state=Mock(), data={
                'id': str(role_id), 'name': str(role_id), 'permissions': str(permissions.value),
                'position': 1, 'colors': {'primary_color': 0, 'secondary_color': None, 'tertiary_color': None}, 'color': 0, 'hoist': False, 'managed': False, 'mentionable': False,
            })
        self.guild.get_role = self.roles.get
        self.guild.default_role = self.roles[GUILD_ID]
        self.guild.me = self.member(90, [40])
        self.applicant = self.member(91, [])
        self.db = SimpleNamespace(get_application_staff_roles=AsyncMock(return_value=[
            {'server_name': 'Seattle', 'role_id': 10},
            {'server_name': 'Seattle', 'role_id': 11},
            {'server_name': 'Orlando', 'role_id': 20},
            {'server_name': 'Orlando', 'role_id': 21},
        ]))
        self.bot = SimpleNamespace(database=self.db)
        self.cog = ApplicationsCog(self.bot)
        self.application = {'guild_id': GUILD_ID, 'game_server': 'Seattle'}

    def member(self, user_id, role_ids):
        member = Mock(spec=disnake.Member)
        member.id, member.guild = user_id, self.guild
        member.roles = [self.roles[GUILD_ID], *(self.roles[rid] for rid in role_ids)]
        member._roles = disnake.utils.SnowflakeList(role_ids)
        member.current_timeout = None
        member.guild_permissions = disnake.Permissions(administrator=40 in role_ids)
        return member

    def channel(self, overwrites):
        data = []
        for target, overwrite in overwrites.items():
            allow, deny = overwrite.pair()
            data.append({'id': str(target.id), 'type': 0 if isinstance(target, disnake.Role) else 1,
                         'allow': str(allow.value), 'deny': str(deny.value)})
        return disnake.TextChannel(state=Mock(), guild=self.guild, data={
            'id': '100', 'name': 'application', 'type': 0, 'position': 0, 'permission_overwrites': data,
        })

    async def test_discord_permissions_isolate_staff_despite_global_view_permission(self):
        staff = await self.cog.application_staff_roles(self.guild, 'Seattle')
        channel = self.channel(self.cog.ticket_overwrites(self.guild, self.applicant, staff))
        for role_ids, allowed in (([10], True), ([11], True), ([20], False),
                                  ([21], False), ([30], False), ([], False), ([40], True)):
            with self.subTest(roles=role_ids):
                self.assertEqual(channel.permissions_for(self.member(92, role_ids)).view_channel, allowed)
        self.assertTrue(channel.permissions_for(self.applicant).view_channel)
        self.assertTrue(channel.permissions_for(self.guild.me).view_channel)

    async def test_buttons_reject_other_server_and_generic_high_even_at_level_four(self):
        with patch('access_control.member_access_level', AsyncMock(return_value=4)):
            for roles, allowed in (([10], True), ([11], True), ([20], False), ([21], False), ([30], False)):
                self.assertEqual(await can_review_application(self.bot, self.member(92, roles), self.application), allowed)

    async def test_admin_and_developer_exemptions_still_require_same_guild(self):
        for member in (self.member(92, [40]), self.member(DEVELOPER_USER_ID, [])):
            self.assertTrue(await can_review_application(self.bot, member, self.application))
            self.assertFalse(await can_review_application(self.bot, member, dict(self.application, guild_id=123)))

    async def test_legacy_multiple_servers_and_missing_config_fail_closed(self):
        for server in ('Seattle, Orlando', 'Denver'):
            self.assertEqual(await self.cog.application_staff_roles(self.guild, server), [])
            with patch('access_control.member_access_level', AsyncMock(return_value=4)):
                self.assertFalse(await can_review_application(self.bot, self.member(92, [10, 20]),
                                                             dict(self.application, game_server=server)))
        del self.roles[11]
        self.assertEqual(await self.cog.application_staff_roles(self.guild, 'Seattle'), [])

    async def test_configuration_commands_cannot_be_lowered(self):
        for name in ('applications roles', 'applications roles_show', 'applications reset_cooldown'):
            self.assertEqual(await command_access_level(self.bot, GUILD_ID, name), 5)

    async def test_archive_revokes_old_role_and_member_grants(self):
        archive = Mock(spec=disnake.TextChannel)
        archive.guild = self.guild
        archive.overwrites = {self.roles[20]: disnake.PermissionOverwrite(view_channel=True),
                              self.applicant: disnake.PermissionOverwrite(view_channel=True)}
        archive.edit = AsyncMock()
        self.bot.get_channel = Mock(return_value=archive)
        self.assertIs(await application_log_channel(self.bot), archive)
        channel = self.channel(archive.edit.call_args.kwargs['overwrites'])
        self.assertFalse(channel.permissions_for(self.applicant).view_channel)
        for role_id in (10, 11, 20, 21, 30):
            self.assertFalse(channel.permissions_for(self.member(92, [role_id])).view_channel)
        self.assertTrue(channel.permissions_for(self.member(92, [40])).view_channel)

    async def test_archive_permission_failure_prevents_sending_sensitive_log(self):
        archive = Mock(spec=disnake.TextChannel)
        archive.guild = self.guild
        archive.overwrites = {}
        archive.edit = AsyncMock(side_effect=disnake.DiscordException('denied'))
        archive.send = AsyncMock()
        self.bot.get_channel = Mock(return_value=archive)
        from config import APPLICATION_LOG_CHANNEL_ID
        with self.assertLogs(level='ERROR'):
            await send_admin_log(self.bot, channel_id=APPLICATION_LOG_CHANNEL_ID, title='private', description='private')
        archive.send.assert_not_awaited()

    async def test_existing_ticket_revokes_old_grants_before_sanitizing_and_regranting(self):
        for sanitized in (True, False):
            with self.subTest(sanitized=sanitized):
                application = dict(self.application, id=1, discord_id=91,
                                   ticket_channel_id=100, ticket_message_id=101)
                channel = Mock(spec=disnake.TextChannel)
                channel.guild = self.guild
                channel.overwrites = {self.roles[20]: disnake.PermissionOverwrite(view_channel=True)}
                events = []

                async def edit(**kwargs):
                    channel.overwrites = kwargs['overwrites']
                    events.append('edit')

                async def sanitize(*args):
                    events.append('sanitize')
                    return sanitized

                channel.edit = AsyncMock(side_effect=edit)
                channel.fetch_message = AsyncMock(return_value=Mock())
                self.guild.get_member = Mock(return_value=self.applicant)
                self.guild.get_channel = Mock(return_value=channel)
                self.bot.get_guild = Mock(return_value=self.guild)
                self.db.get_ticket_applications = AsyncMock(return_value=[application])
                self.cog._sync_application_message = AsyncMock(side_effect=sanitize)
                failures = await self.cog.sync_ticket_access(GUILD_ID)
                self.assertEqual(failures, 0 if sanitized else 1)
                self.assertEqual(events, ['edit', 'sanitize', 'edit'] if sanitized else ['edit', 'sanitize'])
                secured = self.channel(channel.overwrites)
                self.assertFalse(secured.permissions_for(self.member(92, [20])).view_channel)
                self.assertEqual(secured.permissions_for(self.member(92, [10])).view_channel, sanitized)
                self.assertTrue(secured.permissions_for(self.applicant).view_channel)

    async def test_button_cannot_act_on_an_application_from_another_channel(self):
        inter = SimpleNamespace(
            component=SimpleNamespace(custom_id='application:delete:1'),
            author=self.member(92, [40]), guild_id=GUILD_ID, channel_id=999,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        self.db.get_application_config = AsyncMock(return_value={})
        self.db.get_application = AsyncMock(return_value=dict(self.application, ticket_channel_id=100))
        await self.cog.application_button(inter)
        self.assertIn('Обрабатывать заявку', inter.followup.send.call_args.args[0])

    def test_panel_uses_one_family_and_one_server(self):
        embed, _ = application_panel()
        self.assertEqual(embed.title, 'INTERPOLOV')
        self.assertEqual(server_select(1, [{'name': 'Seattle', 'id': 1}, {'name': 'Orlando', 'id': 2}]).max_values, 1)


if __name__ == '__main__':
    unittest.main()
