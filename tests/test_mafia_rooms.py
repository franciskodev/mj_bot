import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from weakref import WeakValueDictionary
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
    @property
    def members(self):
        return [member for member in self.guild.members if self in member.roles]


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
        self.fail_role_commit = False
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
        if self.fail_role_commit and game['state'].get('participant_role_id'):
            self.fail_role_commit = False
            raise RuntimeError('simulated crash before role commit')
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
    guild.members = list(members.values())
    guild.fetch_member = AsyncMock(side_effect=lambda uid: members[uid])
    guild.fetch_roles = AsyncMock(side_effect=lambda: list(roles.values()))
    async def create_role(**kwargs):
        role = Role(10000 + len(roles), 2)
        role.guild, role.name, role.permissions = guild, kwargs['name'], kwargs['permissions']
        roles[role.id] = role
        async def delete_role(**kwargs):
            guild.order.append(role.id)
            del roles[role.id]
            for member in members.values():
                member.roles = [r for r in member.roles if r.id != role.id]
        role.delete = AsyncMock(side_effect=delete_role)
        return role
    guild.create_role = AsyncMock(side_effect=create_role)
    channels = []
    guild.get_channel = lambda cid: next((c for c in channels if c.id == cid), None)
    guild.fetch_channels = AsyncMock(side_effect=lambda: list(channels))
    async def create_category(name, **kwargs):
        category = Mock(spec=disnake.CategoryChannel)
        category.id, category.name, category.category_id = len(channels) + 1000, name, None
        category.overwrites = kwargs['overwrites']
        channels.append(category)
        async def delete_category(**kwargs):
            guild.order.append(category.id)
            channels.remove(category)
        category.delete = AsyncMock(side_effect=delete_category)
        async def child(name, voice=False, **kw):
            channel = Mock(spec=disnake.VoiceChannel if voice else disnake.TextChannel)
            channel.id, channel.name, channel.category_id = len(channels) + 1000, name, category.id
            channel.guild = guild
            channel.overwrites = kw['overwrites']
            channels.append(channel)
            async def delete_child(**kwargs):
                guild.order.append(channel.id)
                channels.remove(channel)
            channel.delete = AsyncMock(side_effect=delete_child)
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
    async def test_participant_role_tracks_members_and_keeps_eliminated_players(self):
        bot, guild, members, _, _ = environment()
        manager = MafiaRooms(bot)
        rules.join(bot.database.game['state'], 1, 'one')
        rules.join(bot.database.game['state'], 2, 'two')
        game = await manager.ensure_rooms(await bot.database.get_mafia_game())
        role = guild.get_role(game['state']['participant_role_id'])
        self.assertEqual(role.permissions.value, 0)
        self.assertFalse(guild.create_role.call_args.kwargs['mentionable'])
        self.assertEqual({m.id for m in role.members}, {1, 2})
        async with bot.database.mafia_transaction() as current:
            rules.leave(current['state'], 1)
            rules.join(current['state'], 3, 'three')
            current['state']['players']['2']['alive'] = False
        game = await manager.sync_participant_role(await bot.database.get_mafia_game())
        self.assertEqual({m.id for m in role.members}, {2, 3})
        self.assertEqual(game['state']['participant_role_members'], ['2', '3'])
        members[3].roles = [r for r in members[3].roles if r.id != FAMILY_MEMBER_ROLE_ID]
        await MafiaRooms(bot).sync_participant_role(game)
        self.assertEqual({m.id for m in role.members}, {2})
        guild.create_role.assert_awaited_once()

    async def test_participant_role_creation_recovers_before_database_commit(self):
        bot, guild, _, _, _ = environment()
        bot.database.fail_role_commit = True
        with self.assertRaises(RuntimeError):
            await MafiaRooms(bot).ensure_rooms(await bot.database.get_mafia_game())
        self.assertNotIn('participant_role_id', bot.database.game['state'])
        game = await MafiaRooms(bot).ensure_rooms(await bot.database.get_mafia_game())
        self.assertIsNotNone(guild.get_role(game['state']['participant_role_id']))
        guild.create_role.assert_awaited_once()

    async def test_events_ping_only_party_role_and_do_not_repeat_after_restart(self):
        bot, guild, _, _, _ = environment()
        game = await MafiaRooms(bot).ensure_rooms(await bot.database.get_mafia_game())
        bot.get_channel = guild.get_channel
        bot.user = guild.me
        channel = guild.get_channel(game['channel_id'])
        messages = []
        async def history(**kwargs):
            for message in messages:
                yield message
        async def send(**kwargs):
            message = SimpleNamespace(id=len(messages) + 2000, author=bot.user, embeds=[kwargs['embed']])
            messages.append(message)
            return message
        channel.history = history
        channel.send = AsyncMock(side_effect=send)
        events = [dict(id=i, payload=json.dumps(dict(event='<@1> @everyone', phase=phase,
                  round=i, paused=False, winner=winner)))
                  for i, phase, winner in [(1, 'lobby', None), (2, 'night', None), (3, 'finished', 'town')]]
        bot.database.get_mafia_events = AsyncMock(return_value=events)
        bot.database.mark_mafia_event_sent = AsyncMock()
        await MafiaRooms(bot).send_events(game)
        self.assertEqual(channel.send.await_count, 3)
        for call in channel.send.await_args_list:
            self.assertEqual(call.kwargs['content'], f"<@&{game['state']['participant_role_id']}>")
            self.assertEqual(call.kwargs['allowed_mentions'].to_dict(), {
                'parse': [], 'roles': [game['state']['participant_role_id']],
            })
        self.assertTrue(channel.overwrites[guild.me].mention_everyone)
        # Simulate a restart after Discord accepted messages but before DB acknowledgement.
        await MafiaRooms(bot).send_events(await bot.database.get_mafia_game())
        self.assertEqual(channel.send.await_count, 3)
        self.assertEqual(bot.database.mark_mafia_event_sent.await_count, 6)

    async def test_participant_role_cleanup_failure_retries_after_restart(self):
        bot, guild, _, _, channels = environment()
        game = await MafiaRooms(bot).ensure_rooms(await bot.database.get_mafia_game())
        role = guild.get_role(game['state']['participant_role_id'])
        delete = role.delete.side_effect
        role.delete.side_effect = disnake.DiscordException('temporary role deletion failure')
        async with bot.database.mafia_transaction() as current:
            rules.cancel(current['state'])
            current['state']['cleanup_at'] = 0
        with self.assertLogs(level='ERROR'):
            self.assertFalse(await MafiaRooms(bot).delete_game_rooms(await bot.database.get_mafia_game()))
        self.assertFalse(bot.database.game['state'].get('rooms_deleted', False))
        self.assertEqual([c.id for c in channels], [game['state']['rooms']['category']])
        role.delete.side_effect = delete
        self.assertTrue(await MafiaRooms(bot).delete_game_rooms(await bot.database.get_mafia_game()))
        self.assertIsNone(guild.get_role(role.id))
        self.assertEqual(channels, [])

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
        for phase in ('lobby', 'night', 'day', 'vote', 'night', 'day'):
            for paused in (False, True):
                with self.subTest(phase=phase, paused=paused):
                    state.update(phase=phase, paused=paused)
                    voice = room_overwrites(guild, state, 'discussion_voice', family_ids)
                    discussion = room_overwrites(guild, state, 'discussion', family_ids)
                    can_discuss = phase in {'lobby', 'day'} and not paused
                    for member in members.values():
                        self.assertTrue(voice[member].connect)
                        self.assertEqual(voice[member].speak, can_discuss)
                        self.assertEqual(voice[member].stream, can_discuss)
                        self.assertEqual(discussion[member].send_messages, can_discuss)
                        self.assertFalse(discussion[member].send_messages_in_threads)
        state.update(phase='day', paused=False)
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

    async def test_cleanup_removes_game_rooms_then_category_only(self):
        bot, guild, _, _, channels = environment()
        manager = MafiaRooms(bot)
        game = await manager.ensure_rooms(await bot.database.get_mafia_game())
        for category_id in (None, game['state']['rooms']['category']):
            other = Mock(spec=disnake.TextChannel)
            other.id, other.name, other.category_id = 500 + len(channels), 'unrelated', category_id
            other.delete = AsyncMock()
            channels.append(other)
        async with bot.database.mafia_transaction() as current:
            rules.cancel(current['state'])
            current['state']['cleanup_at'] = 0
        game = await bot.database.get_mafia_game()
        self.assertTrue(await manager.delete_game_rooms(game))
        self.assertEqual(guild.order[-1], game['state']['rooms']['category'])
        self.assertEqual(set(guild.order), set(game['state']['rooms'].values()) | {game['state']['participant_role_id']})
        self.assertEqual(len(channels), 2)
        for channel in channels:
            channel.delete.assert_not_awaited()
        self.assertTrue(bot.database.game['state']['rooms_deleted'])

    async def test_cleanup_failure_retries_after_restart(self):
        bot, guild, _, _, channels = environment()
        game = await MafiaRooms(bot).ensure_rooms(await bot.database.get_mafia_game())
        stuck = channels[1]
        delete = stuck.delete.side_effect
        stuck.delete.side_effect = disnake.DiscordException('temporary failure')
        async with bot.database.mafia_transaction() as current:
            rules.cancel(current['state'])
            current['state']['cleanup_at'] = 0
        with self.assertLogs(level='ERROR'):
            self.assertFalse(await MafiaRooms(bot).delete_game_rooms(await bot.database.get_mafia_game()))
        self.assertFalse(bot.database.game['state'].get('rooms_deleted', False))
        self.assertEqual({channel.id for channel in channels}, {stuck.id, game['state']['rooms']['category']})
        stuck.delete.side_effect = delete
        self.assertTrue(await MafiaRooms(bot).delete_game_rooms(await bot.database.get_mafia_game()))
        self.assertEqual(channels, [])
        self.assertTrue(await MafiaRooms(bot).delete_game_rooms(await bot.database.get_mafia_game()))

    async def test_cleanup_waits_and_never_deletes_active_game(self):
        bot, guild, _, _, _ = environment()
        game = {'id': 1, 'state': started_state()}
        game['state']['cleanup_at'] = 0
        self.assertFalse(await MafiaRooms(bot).delete_game_rooms(game))
        rules.finish(game['state'], 'town')
        game['state']['cleanup_at'] = disnake.utils.utcnow().timestamp() + 3600
        self.assertFalse(await MafiaRooms(bot).delete_game_rooms(game))
        guild.fetch_channels.assert_not_awaited()

    async def test_cleanup_recovers_unrecorded_rooms_from_partial_setup(self):
        bot, _, _, _, channels = environment()
        bot.database.fail_room_commit = 'events'
        with self.assertRaises(RuntimeError):
            await MafiaRooms(bot).ensure_rooms(await bot.database.get_mafia_game())
        async with bot.database.mafia_transaction() as game:
            rules.cancel(game['state'])
            game['state']['cleanup_at'] = 0
        self.assertEqual(len(channels), 2)
        self.assertTrue(await MafiaRooms(bot).delete_game_rooms(await bot.database.get_mafia_game()))
        self.assertEqual(channels, [])

    async def test_cleanup_and_role_restoration_are_independent(self):
        bot, _, _, _, _ = environment()
        rules.cancel(bot.database.game['state'])
        bot.database.game['state']['cleanup_at'] = 0
        cog = object.__new__(MafiaCog)
        cog.bot = bot
        cog.rooms = SimpleNamespace(
            restore_admin_roles=AsyncMock(side_effect=RuntimeError('database failure')),
            delete_game_rooms=AsyncMock(), sync_permissions=AsyncMock(), send_events=AsyncMock(),
        )
        with self.assertRaises(RuntimeError):
            await cog.maintain_game(1)
        cog.rooms.delete_game_rooms.assert_awaited_once()
        cog.rooms.sync_permissions.assert_not_awaited()
        cog.rooms.send_events.assert_not_awaited()

    async def test_cleaned_game_does_not_recreate_panel_or_rooms(self):
        bot, _, _, _, _ = environment()
        rules.cancel(bot.database.game['state'])
        bot.database.game['state']['rooms_deleted'] = True
        cog = object.__new__(MafiaCog)
        cog.bot, cog._message_locks = bot, WeakValueDictionary()
        cog.rooms = SimpleNamespace(restore_admin_roles=AsyncMock(), delete_game_rooms=AsyncMock(),
                                    sync_permissions=AsyncMock(), send_events=AsyncMock())
        await cog.maintain_game(1)
        await cog.sync_message(1, force=True)
        cog.rooms.restore_admin_roles.assert_awaited_once_with(1)
        cog.rooms.delete_game_rooms.assert_not_awaited()
        cog.rooms.sync_permissions.assert_not_awaited()
        cog.rooms.send_events.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
