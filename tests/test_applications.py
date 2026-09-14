import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import disnake

from access_control import can_review_application, command_access_level
from cogs.applications import ApplicationModal, ApplicationsCog, application_panel, server_select
from config import BASE_RECRUIT_ROLE_ID, DEVELOPER_USER_ID, GUILD_ID, RECRUITMENT_POSITION_ROLE_IDS
from log_utils import application_log_channel, send_admin_log


class ApplicationAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=GUILD_ID, owner_id=999)
        self.roles = {}
        for role_id in (GUILD_ID, 10, 11, 20, 21, 30, 40, BASE_RECRUIT_ROLE_ID, *RECRUITMENT_POSITION_ROLE_IDS.values()):
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
            {'server_name': 'Seattle', 'role_id': 10, 'access_level': 2},
            {'server_name': 'Seattle', 'role_id': 11, 'access_level': 4},
            {'server_name': 'Orlando', 'role_id': 20, 'access_level': 2},
            {'server_name': 'Orlando', 'role_id': 21, 'access_level': 4},
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
        for name in ('applications roles', 'applications roles_show', 'applications reset_cooldown',
                     'applications recruiter_setup'):
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

    async def test_recruiter_ticket_is_reviewed_only_by_chiefs(self):
        staff = await self.cog.application_staff_roles(self.guild, 'Seattle', 'recruiter')
        channel = self.channel(self.cog.ticket_overwrites(self.guild, self.applicant, staff))
        application = dict(self.application, application_type='recruiter', discord_id=91)
        with patch('access_control.member_access_level', AsyncMock(return_value=4)):
            for role_ids, allowed in (([10], False), ([11], False), ([20], False),
                                      ([21], False), ([30], False), ([40], False),
                                      *[([role_id], True) for role_id in RECRUITMENT_POSITION_ROLE_IDS.values()]):
                with self.subTest(roles=role_ids):
                    member = self.member(92, role_ids)
                    self.assertEqual(channel.permissions_for(member).view_channel, allowed or 40 in role_ids)
                    self.assertEqual(await can_review_application(self.bot, member, application), allowed)
            self.assertFalse(await can_review_application(self.bot, self.member(91, list(RECRUITMENT_POSITION_ROLE_IDS.values())), application))
        self.assertTrue(channel.permissions_for(self.applicant).view_channel)
        self.assertFalse(await can_review_application(self.bot, self.member(DEVELOPER_USER_ID, []), application))

    async def test_recruiter_button_and_server_choice_open_separate_form(self):
        self.db.is_application_blocked = AsyncMock(return_value=False)
        self.db.get_application_cooldown_until = AsyncMock(return_value=None)
        self.db.has_open_application = AsyncMock(return_value=False)
        family = {'id': 1, 'name': 'INTERPOLOV'}
        self.db.get_family_by_name = AsyncMock(return_value=family)
        self.db.get_family = AsyncMock(return_value=family)
        self.db.get_game_servers = AsyncMock(return_value=[{'id': 2, 'name': 'Seattle', 'role_id': 30}])
        self.db.get_recruiter_application_config = AsyncMock(return_value={'category_id': 100})
        self.db.get_application_config = AsyncMock()
        _, buttons = application_panel('recruiter')
        inter = SimpleNamespace(
            component=buttons[0], author=self.applicant, guild_id=GUILD_ID, guild=self.guild,
            response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()),
        )
        await self.cog.application_button(inter)
        choice = inter.response.send_message.call_args.kwargs['components']
        self.assertEqual(choice.custom_id, 'recruiter_application:server:1')
        self.assertEqual(choice.max_values, 1)
        inter.component, inter.values = choice, ['2']
        await self.cog.application_server_select(inter)
        modal = inter.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, ApplicationModal)
        self.assertEqual(modal.application_type, 'recruiter')
        self.assertEqual(modal.game_server, 'Seattle')
        self.db.get_application_cooldown_until.assert_awaited_with(GUILD_ID, 91, 'recruiter')
        self.db.has_open_application.assert_awaited_with(91, GUILD_ID, 'recruiter')
        self.db.get_application_config.assert_not_awaited()

    def prepare_recruiter_acceptance(self):
        application = dict(self.application, id=1, application_type='recruiter', discord_id=91,
                           ticket_channel_id=100, status='pending', static_id='123', family_name='INTERPOLOV')
        self.db.get_application = AsyncMock(return_value=application)
        self.db.get_recruiter_application_config = AsyncMock(return_value={'category_id': 100})
        self.db.find_blacklist_entries = AsyncMock(return_value=[])
        self.db.review_application = AsyncMock(return_value=dict(application, status='accepted'))
        self.db.save_users = AsyncMock()
        self.guild.get_member = Mock(return_value=self.applicant)
        self.guild.fetch_member = AsyncMock(return_value=self.applicant)
        self.roles[40].position = 10
        self.guild.me.top_role = self.roles[40]
        self.guild.me.guild_permissions = disnake.Permissions(manage_roles=True)

        async def add_roles(*roles, **kwargs):
            self.applicant.roles.extend(roles)

        self.applicant.add_roles = AsyncMock(side_effect=add_roles)
        self.applicant.send = AsyncMock()
        reviewer = self.member(92, [11, RECRUITMENT_POSITION_ROLE_IDS['Chief Recruit']])
        self.roles[11].position = 5
        reviewer.top_role = self.roles[11]
        inter = SimpleNamespace(
            component=SimpleNamespace(custom_id='application:accepted:1'),
            author=reviewer, guild_id=GUILD_ID, guild=self.guild, channel_id=100,
            channel=Mock(spec=disnake.TextChannel), message=Mock(),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
        )
        self.cog._sync_application_message = AsyncMock(return_value=True)
        self.cog._finalize_ticket = AsyncMock(return_value=True)
        return inter

    async def test_recruiter_acceptance_grants_server_and_base_recruit_roles(self):
        inter = self.prepare_recruiter_acceptance()
        with patch('access_control.member_access_level', AsyncMock(return_value=4)), \
                patch('cogs.applications.send_admin_log', AsyncMock()):
            await self.cog.application_button(inter)
        self.assertEqual(self.applicant.add_roles.call_args.args, (self.roles[10], self.roles[BASE_RECRUIT_ROLE_ID]))
        self.db.review_application.assert_awaited_once_with(1, 'accepted', 92)
        self.db.save_users.assert_not_awaited()

    async def test_recruiter_acceptance_requires_assignable_base_role(self):
        inter = self.prepare_recruiter_acceptance()
        base_role = self.roles.pop(BASE_RECRUIT_ROLE_ID)
        await self.cog.application_button(inter)
        self.applicant.add_roles.assert_not_awaited()
        self.db.review_application.assert_not_awaited()
        self.roles[BASE_RECRUIT_ROLE_ID] = base_role
        base_role.managed = True
        await self.cog.application_button(inter)
        self.applicant.add_roles.assert_not_awaited()
        self.db.review_application.assert_not_awaited()
        base_role.managed = False
        base_role.position = 20
        await self.cog.application_button(inter)
        self.applicant.add_roles.assert_not_awaited()
        self.db.review_application.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
