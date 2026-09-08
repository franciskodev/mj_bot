import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import disnake

from access_control import has_family_role, member_access_level
from cogs import mafia_game as rules
from cogs.mafia import MafiaCog, private_panel
from cogs.mafia_rooms import MafiaRooms, admin_roles_to_suspend, room_overwrites
from config import DEVELOPER_USER_ID, FAMILY_MEMBER_ROLE_ID, GUILD_ID


class Role:
    def __init__(self, role_id, position, *, administrator=False, managed=False):
        self.id, self.position, self.managed = role_id, position, managed
        self.permissions = disnake.Permissions(administrator=administrator)
        self.name = str(role_id)
    def __lt__(self, other):
        return self.position < other.position
    def __ge__(self, other):
        return self.position >= other.position
    def is_default(self):
        return self.position == 0


class Member:
    def __init__(self, uid, guild, roles):
        self.id, self.guild, self.roles = uid, guild, roles
        self.mention, self.display_name = f'<@{uid}>', str(uid)
        self.bot, self.voice = False, None
        self.fail_remove = self.fail_add = False
    @property
    def top_role(self):
        return max(self.roles, key=lambda r: r.position)
    @property
    def guild_permissions(self):
        return disnake.Permissions.all() if any(r.permissions.administrator for r in self.roles) else disnake.Permissions.none()
    async def remove_roles(self, *roles, **kwargs):
        self.guild.order.append('remove')
        if self.fail_remove:
            raise disnake.DiscordException('remove failed')
        self.roles = [r for r in self.roles if r not in roles]
    async def add_roles(self, *roles, **kwargs):
        self.guild.order.append('add')
        if self.fail_add:
            raise disnake.DiscordException('add failed')
        self.roles.extend(r for r in roles if r not in self.roles)


class MemoryDB:
    def __init__(self, guild):
        self.guild = guild
        self.holds = {}
        self.game = {'id': 1, 'guild_id': GUILD_ID, 'channel_id': 99, 'state': rules.new_game(1)}
        self.fail_room_commit = None
    async def get_families(self, guild_id):
        return []
    async def get_highest_role_access_level(self, *args):
        return 0
    async def get_department_configs(self, *args):
        return []
    async def get_mafia_saved_access(self, guild_id, uid):
        return self.holds.get(uid, {}).get('access_level', 0)
    async def hold_mafia_roles(self, game_id, guild_id, uid, role_ids, level):
        self.guild.order.append('save')
        self.holds[uid] = {'discord_id': uid, 'role_ids': role_ids, 'access_level': level}
    async def get_mafia_role_holds(self, game_id):
        return list(self.holds.values())
    async def finish_mafia_role_hold(self, game_id, uid):
        self.guild.order.append('confirmed')
        del self.holds[uid]
    async def get_mafia_game(self, *args):
        return copy.deepcopy(self.game)
    @asynccontextmanager
    async def mafia_transaction(self, *args):
        game = copy.deepcopy(self.game)
        yield game
        if self.fail_room_commit and self.fail_room_commit in game['state'].get('rooms', {}):
            self.fail_room_commit = None
            raise RuntimeError('simulated crash before commit')
        self.game = game
        self.game['channel_id'] = game['state'].get('rooms', {}).get('events', game['channel_id'])


def environment():
    guild = SimpleNamespace(id=GUILD_ID, owner_id=999, order=[])
    everyone = Role(GUILD_ID, 0)
    family = Role(FAMILY_MEMBER_ROLE_ID, 1)
    admin = Role(100, 3, administrator=True)
    bot_role = Role(101, 10, administrator=True)
    guild.default_role = everyone
    roles = {r.id: r for r in (everyone, family, admin, bot_role)}
    guild.get_role = roles.get
    members = {uid: Member(uid, guild, [everyone, family]) for uid in range(1, 7)}
    members[1].roles.append(admin)
    guild.me = Member(777, guild, [everyone, bot_role])
    guild.get_member = members.get
    guild.fetch_member = AsyncMock(side_effect=lambda uid: members[uid])
    channels = []
    guild.get_channel = lambda cid: next((c for c in channels if c.id == cid), None)
    guild.fetch_channels = AsyncMock(side_effect=lambda: list(channels))
    async def create_category(name, **kwargs):
        category = Mock(spec=disnake.CategoryChannel)
        category.id, category.name, category.category_id = len(channels) + 1000, name, None
        category.overwrites = kwargs['overwrites']
        channels.append(category)
        async def child(name, voice=False, **kw):
            channel = Mock(spec=disnake.VoiceChannel if voice else disnake.TextChannel)
            channel.id, channel.name, channel.category_id = len(channels) + 1000, name, category.id
            channel.guild = guild
            channel.overwrites = kw['overwrites']
            channels.append(channel)
            return channel
        category.create_text_channel = AsyncMock(side_effect=child)
        async def voice_child(name, **kw):
            return await child(name, voice=True, **kw)
        category.create_voice_channel = AsyncMock(side_effect=voice_child)
        return category
    guild.create_category = AsyncMock(side_effect=create_category)
    db = MemoryDB(guild)
    bot = SimpleNamespace(database=db, get_guild=lambda gid: guild)
    return bot, guild, members, admin, channels


def started_state():
    state = rules.new_game(1)
    for uid in range(1, 7):
        rules.join(state, uid, str(uid))
    rules.start(state, 100)
    for uid, role in zip(state['players'], ('mafia', 'mafia', 'doctor', 'commissioner', 'civilian', 'civilian')):
        state['players'][uid]['role'] = role
    return state


class RoomTests(unittest.IsolatedAsyncioTestCase):
    async def test_family_role_required_even_for_administrator(self):
        bot, guild, members, admin, _ = environment()
        self.assertTrue(await has_family_role(bot, members[1]))
        members[1].roles = [guild.default_role, admin]
        self.assertFalse(await has_family_role(bot, members[1]))
        self.assertEqual(await member_access_level(bot, members[1]), 5)

    async def test_private_rooms_and_dead_player_permissions(self):
        _, guild, members, _, _ = environment()
        state = started_state()
        family_ids = {FAMILY_MEMBER_ROLE_ID}
        mafia = room_overwrites(guild, state, 'mafia_text', family_ids)
        self.assertTrue(mafia[members[1]].view_channel)
        self.assertTrue(mafia[members[1]].send_messages)
        self.assertFalse(mafia[members[5]].view_channel)
        self.assertNotIn(guild.get_role(FAMILY_MEMBER_ROLE_ID), mafia)
        voice = room_overwrites(guild, state, 'discussion_voice', family_ids)
        self.assertTrue(voice[members[5]].connect)
        self.assertFalse(voice[members[5]].speak)
        state['phase'] = 'day'
        voice = room_overwrites(guild, state, 'discussion_voice', family_ids)
        self.assertTrue(voice[members[5]].speak)
        state['players']['1']['alive'] = False
        self.assertFalse(room_overwrites(guild, state, 'mafia_text', family_ids)[members[1]].view_channel)
        self.assertFalse(room_overwrites(guild, state, 'discussion_voice', family_ids)[members[1]].connect)
        self.assertTrue(room_overwrites(guild, state, 'dead_voice', family_ids)[members[1]].connect)
        events = room_overwrites(guild, state, 'events', family_ids)
        self.assertFalse(events[members[1]].send_messages)
        self.assertFalse(events[members[1]].manage_roles)
        self.assertFalse(events[members[1]].manage_channels)

    async def test_preparation_hides_mafia_and_role_cards(self):
        _, guild, members, _, _ = environment()
        state = started_state()
        state.update(setup_pending=True, paused=True, deadline=None)
        mafia = room_overwrites(guild, state, 'mafia_text', {FAMILY_MEMBER_ROLE_ID})
        self.assertFalse(mafia[members[1]].view_channel)
        with self.assertRaises(rules.GameError):
            private_panel({'state': state}, 1)
        with self.assertRaises(rules.GameError):
            rules.toggle_pause(state, 120)

    async def test_snapshot_before_removal_and_restore_after_restart(self):
        bot, guild, members, admin, _ = environment()
        manager = MafiaRooms(bot)
        game = {'id': 1, 'state': started_state()}
        await manager.suspend_admin_roles(game)
        self.assertEqual(guild.order, ['save', 'remove'])
        self.assertNotIn(admin, members[1].roles)
        self.assertEqual(await member_access_level(bot, members[1]), 5)
        self.assertTrue(await has_family_role(bot, members[1]))
        new_manager = MafiaRooms(bot)
        await new_manager.restore_admin_roles(1)
        self.assertIn(admin, members[1].roles)
        self.assertEqual(guild.order[-2:], ['add', 'confirmed'])
        self.assertEqual(bot.database.holds, {})

    async def test_failed_role_api_keeps_recovery_record(self):
        bot, guild, members, admin, _ = environment()
        manager = MafiaRooms(bot)
        members[1].fail_remove = True
        with self.assertRaises(disnake.DiscordException):
            await manager.suspend_admin_roles({'id': 1, 'state': started_state()})
        self.assertIn(1, bot.database.holds)
        members[1].fail_remove = False
        await manager.suspend_admin_roles({'id': 1, 'state': started_state()})
        members[1].fail_add = True
        with self.assertLogs(level='ERROR'):
            await manager.restore_admin_roles(1)
        self.assertIn(1, bot.database.holds)
        members[1].fail_add = False
        await manager.restore_admin_roles(1)
        self.assertNotIn(1, bot.database.holds)
        self.assertIn(admin, members[1].roles)

    async def test_hierarchy_and_managed_roles_apply_to_developer(self):
        _, guild, members, admin, _ = environment()
        family_ids = {FAMILY_MEMBER_ROLE_ID}
        admin.position = 11
        with self.assertRaises(rules.GameError):
            admin_roles_to_suspend(members[1], family_ids)
        admin.position = 3
        admin.managed = True
        with self.assertRaises(rules.GameError):
            admin_roles_to_suspend(members[1], family_ids)
        members[1].id = DEVELOPER_USER_ID
        with self.assertRaises(rules.GameError):
            admin_roles_to_suspend(members[1], family_ids)
        admin.managed = False
        self.assertEqual(admin_roles_to_suspend(members[1], family_ids), [admin])
        admin.position = 11
        with self.assertRaises(rules.GameError):
            admin_roles_to_suspend(members[1], family_ids)
        admin.position = 3
        members[1].id = guild.owner_id
        with self.assertRaises(rules.GameError):
            admin_roles_to_suspend(members[1], family_ids)

    async def test_developer_roles_restored_and_level_six_preserved(self):
        bot, guild, members, admin, _ = environment()
        developer = members.pop(1)
        developer.id = DEVELOPER_USER_ID
        members[developer.id] = developer
        state = started_state()
        state['players'][str(developer.id)] = state['players'].pop('1')
        await MafiaRooms(bot).suspend_admin_roles({'id': 1, 'state': state})
        self.assertNotIn(admin, developer.roles)
        self.assertFalse(developer.guild_permissions.administrator)
        self.assertEqual(bot.database.holds[developer.id]['access_level'], 6)
        self.assertEqual(await member_access_level(bot, developer), 6)
        developer.fail_add = True
        with self.assertLogs(level='ERROR'):
            await MafiaRooms(bot).restore_admin_roles(1)
        self.assertIn(developer.id, bot.database.holds)
        developer.fail_add = False
        await MafiaRooms(bot).restore_admin_roles(1)
        self.assertIn(admin, developer.roles)
        self.assertNotIn(developer.id, bot.database.holds)
        self.assertEqual(await member_access_level(bot, developer), 6)

    async def test_provisioning_resumes_without_duplicate_rooms(self):
        bot, guild, _, _, channels = environment()
        manager = MafiaRooms(bot)
        bot.database.fail_room_commit = 'events'
        with self.assertRaises(RuntimeError):
            await manager.ensure_rooms(await bot.database.get_mafia_game())
        self.assertEqual(len(channels), 2)
        restored = MafiaRooms(bot)
        game = await restored.ensure_rooms(await bot.database.get_mafia_game())
        self.assertEqual(len(channels), 7)  # category, three text channels, three voices
        self.assertEqual(len(game['state']['rooms']), 7)
        self.assertEqual(game['channel_id'], game['state']['rooms']['events'])
        await restored.ensure_rooms(game)
        self.assertEqual(len(channels), 7)
        guild.create_category.assert_awaited_once()

    async def test_restore_is_independent_of_channel_failures(self):
        bot, _, _, _, _ = environment()
        state = started_state()
        rules.finish(state, 'town')
        state['rooms'] = {'events': 123}
        bot.database.game['state'] = state
        cog = object.__new__(MafiaCog)
        cog.bot, cog._room_sync = bot, {}
        cog.rooms = SimpleNamespace(sync_permissions=AsyncMock(side_effect=disnake.DiscordException('channel unavailable')),
                                    restore_admin_roles=AsyncMock(), send_events=AsyncMock())
        with self.assertRaises(disnake.DiscordException):
            await cog.maintain_game(1)
        cog.rooms.restore_admin_roles.assert_awaited_once_with(1)


if __name__ == '__main__':
    unittest.main()
