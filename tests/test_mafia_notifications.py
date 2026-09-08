import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from weakref import WeakValueDictionary

import disnake

from config import GUILD_ID
from cogs import mafia_game as rules
from cogs.mafia import MafiaCog
from cogs.mafia_notifications import deliver_notifications, personal_events


def lobby():
    state = rules.new_game(1)
    for uid in range(1, 6):
        rules.join(state, uid, str(uid))
    return state


class EventTests(unittest.TestCase):
    def test_confirmation_and_preparation_do_not_expose_roles(self):
        state = lobby()
        before = copy.deepcopy(state)
        rules.request_start(state)
        events = personal_events(before, state)
        self.assertEqual(len(events), 5)
        self.assertTrue(all(e['kind'] == 'confirm' and e['token'] == state['token'] for e in events))
        for uid in range(1, 5):
            rules.confirm_start(state, uid, state['token'])
        before = copy.deepcopy(state)
        rules.confirm_start(state, 5, state['token'])
        rules.start(state, 100)
        state.update(setup_pending=True, paused=True, deadline=None)
        self.assertEqual([e['kind'] for e in personal_events(before, state)], ['confirmed'])
        before = copy.deepcopy(state)
        state.update(setup_pending=False, paused=False, deadline=160)
        events = personal_events(before, state)
        self.assertEqual(len([e for e in events if e['kind'] == 'role']), 5)
        for event in events:
            if event['kind'] == 'role':
                self.assertIn(rules.ROLES[state['players'][str(event['user_id'])]['role']], event['text'])
        self.assertEqual(personal_events(copy.deepcopy(state), state), [])

    def test_selections_and_commissioner_results_only_go_to_actor(self):
        state = lobby()
        rules.start(state, 100)
        for uid, role in zip(state['players'], ('mafia', 'doctor', 'commissioner', 'civilian', 'civilian')):
            state['players'][uid]['role'] = role
        for uid, target in ((1, '4'), (1, '5'), (2, '4'), (3, '1')):
            before = copy.deepcopy(state)
            rules.act(state, uid, target, state['token'], 110)
            events = personal_events(before, state)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]['user_id'], uid)
            self.assertIn(f'<@{target}>', events[0]['text'])
            if uid == 3:
                self.assertIn('**Мафия**', events[0]['text'])
        before = copy.deepcopy(state)
        rules.toggle_pause(state, 120)
        self.assertEqual({e['kind'] for e in personal_events(before, state)}, {'phase'})
        rules.toggle_pause(state, 130)
        rules.enter_phase(state, 'vote', 200)
        for target in ('4', 'skip'):
            before = copy.deepcopy(state)
            rules.act(state, 1, target, state['token'], 210)
            events = personal_events(before, state)
            self.assertEqual([(e['user_id'], e['kind']) for e in events], [(1, 'votes')])

    def test_elimination_and_result_are_personal_and_do_not_repeat(self):
        state = lobby()
        rules.start(state, 100)
        before = copy.deepcopy(state)
        state['players']['5']['alive'] = False
        rules.enter_phase(state, 'day', 160)
        events = personal_events(before, state)
        self.assertEqual([e['user_id'] for e in events if e['kind'] == 'eliminated'], [5])
        before = copy.deepcopy(state)
        rules.cancel(state)
        self.assertEqual([e['kind'] for e in personal_events(before, state)], ['result'] * 5)
        state['rooms_deleted'] = True
        self.assertEqual(personal_events(copy.deepcopy(state), state), [])


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def environment(self, *, enabled=True):
        state = lobby()
        rules.request_start(state)
        row = dict(id=1, game_id=1, discord_id=1,
                   payload=json.dumps(dict(kind='confirm', text='Подтвердите участие', token=state['token'])))
        db = SimpleNamespace(
            get_mafia_notifications=AsyncMock(return_value=[row]),
            get_dm_notifications_enabled=AsyncMock(return_value=enabled),
            get_mafia_game=AsyncMock(return_value=dict(id=1, state=state, channel_id=10, message_id=20)),
            finish_mafia_notification=AsyncMock(), retry_mafia_notification=AsyncMock(),
        )
        user = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=30)))
        bot = SimpleNamespace(database=db, get_user=lambda uid: user)
        return bot, db, user, state

    async def test_confirmation_dm_has_working_button_and_panel_link(self):
        bot, db, user, state = self.environment()
        await deliver_notifications(bot)
        components = user.send.call_args.kwargs['components']
        self.assertEqual(components[0].custom_id, f"mafia:confirm_start:1:{state['token']}")
        self.assertIn(str(GUILD_ID), components[1].url)
        db.finish_mafia_notification.assert_awaited_once_with(1, 30)

    async def test_closed_dms_do_not_block_other_notifications(self):
        bot, db, user, _ = self.environment()
        rows = db.get_mafia_notifications.return_value
        rows.append(dict(rows[0], id=2, discord_id=2))
        user.send.side_effect = [disnake.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Closed DMs'),
                                 SimpleNamespace(id=31)]
        await deliver_notifications(bot)
        self.assertEqual(user.send.await_count, 2)
        self.assertEqual(db.finish_mafia_notification.await_count, 2)
        db.retry_mafia_notification.assert_not_awaited()

    async def test_stale_confirmations_and_disabled_notifications_are_skipped(self):
        for enabled, stale in ((False, False), (True, True)):
            bot, db, user, state = self.environment(enabled=enabled)
            if stale:
                rules.cancel(state)
            await deliver_notifications(bot)
            user.send.assert_not_awaited()
            db.finish_mafia_notification.assert_awaited_once_with(1)

    async def test_transient_discord_error_is_retried(self):
        bot, db, user, _ = self.environment()
        user.send.side_effect = disnake.DiscordException('unavailable')
        await deliver_notifications(bot)
        db.retry_mafia_notification.assert_awaited_once_with(1)
        db.finish_mafia_notification.assert_not_awaited()

    async def test_dm_confirmation_validates_membership_and_starts_only_after_last_player(self):
        state = lobby()
        rules.request_start(state)
        for uid in range(1, 5):
            rules.confirm_start(state, uid, state['token'])
        member = Mock(spec=disnake.Member)
        member.id, member.bot = 5, False
        guild = SimpleNamespace(fetch_member=AsyncMock(return_value=member))
        game = dict(id=1, state=state)

        @asynccontextmanager
        async def transaction(*args):
            yield game

        cog = object.__new__(MafiaCog)
        cog.bot = SimpleNamespace(get_guild=lambda gid: guild,
                                  database=SimpleNamespace(mafia_transaction=transaction))
        cog._game_locks = WeakValueDictionary()
        cog.advance_game, cog.maintain_game, cog.sync_message = AsyncMock(), AsyncMock(), AsyncMock()
        cog.rooms = SimpleNamespace(preflight_start=AsyncMock())
        inter = SimpleNamespace(
            guild_id=None, author=SimpleNamespace(id=5),
            component=SimpleNamespace(custom_id=f"mafia:confirm_start:1:{state['token']}"),
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
        )
        with patch('cogs.mafia.member_access_level', AsyncMock(return_value=1)), \
                patch('cogs.mafia.has_family_role', AsyncMock(return_value=True)):
            await cog.handle_component(inter)
        guild.fetch_member.assert_awaited_once_with(5)
        cog.rooms.preflight_start.assert_awaited_once()
        self.assertEqual(state['phase'], 'night')
        self.assertTrue(state['setup_pending'])
        self.assertIsNone(state['deadline'])
        cog.maintain_game.assert_awaited_once_with(1)
