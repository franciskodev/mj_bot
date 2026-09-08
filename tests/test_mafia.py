import copy
import unittest
from weakref import WeakValueDictionary
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import disnake
from access_control import command_access_level
from cogs import mafia_game as rules
from cogs.mafia import game_buttons, game_embed, private_panel, MafiaCog
from config import GUILD_ID


def playing():
    state = rules.new_game(1)
    for uid in range(1, 7):
        rules.join(state, uid, f'Player {uid}')
    rules.start(state, 100)
    for uid, role in zip(state['players'], ('mafia', 'mafia', 'doctor', 'commissioner', 'civilian', 'civilian')):
        state['players'][uid]['role'] = role
    return state


class RulesTests(unittest.TestCase):
    def test_start_requires_five_and_every_confirmation_before_revealing_roles(self):
        state = rules.new_game(1)
        for uid in range(1, 5):
            rules.join(state, uid, str(uid))
        with self.assertRaises(rules.GameError):
            rules.request_start(state)
        rules.join(state, 5, '5')
        rules.request_start(state)
        token = state['token']
        self.assertIsNone(state['deadline'])
        self.assertFalse(rules.advance(state, 999999))
        for uid in range(1, 5):
            self.assertFalse(rules.confirm_start(state, uid, token))
        self.assertFalse(rules.confirm_start(state, 1, token))
        self.assertEqual(len(state['confirmations']), 4)
        self.assertTrue(all(p['role'] is None for p in state['players'].values()))
        with self.assertRaises(rules.GameError):
            rules.start(state, 100)
        with self.assertRaises(rules.GameError):
            rules.confirm_start(state, 99, token)
        with self.assertRaises(rules.GameError):
            rules.join(state, 6, '6')
        state = copy.deepcopy(state)  # persisted readiness survives reconstruction
        self.assertTrue(rules.confirm_start(state, 5, token))
        rules.start(state, 100)
        self.assertEqual(state['phase'], 'night')
        self.assertEqual(sum(p['role'] == 'mafia' for p in state['players'].values()), 1)
        with self.assertRaises(rules.GameError):
            rules.confirm_start(state, 5, token)

    def test_departure_resets_readiness_and_old_buttons(self):
        for remove in (rules.leave, rules.depart):
            state = rules.new_game(1)
            for uid in range(1, 7):
                rules.join(state, uid, str(uid))
            rules.request_start(state)
            token = state['token']
            rules.confirm_start(state, 1, token)
            remove(state, 6)
            self.assertFalse(state['confirmation_pending'])
            self.assertEqual(state['confirmations'], [])
            rules.request_start(state)
            with self.assertRaises(rules.GameError):
                rules.confirm_start(state, 1, token)
            buttons = game_buttons({'id': 1, 'state': state})
            self.assertEqual(buttons[0].custom_id, f"mafia:confirm_start:1:{state['token']}")
            self.assertNotIn('mafia:start:1', [b.custom_id for b in buttons])
            rules.cancel(state)
            self.assertFalse(state['confirmation_pending'])

    def test_lobby_capacity_and_duplicate(self):
        state = rules.new_game(1)
        with self.assertRaises(rules.GameError):
            rules.start(state, 0)
        for uid in range(12):
            rules.join(state, uid, str(uid))
        with self.assertRaises(rules.GameError):
            rules.join(state, 12, 'extra')
        with self.assertRaises(rules.GameError):
            rules.join(state, 1, 'duplicate')
        rules.leave(state, 1)
        rules.join(state, 12, 'replacement')
        self.assertEqual(len(state['players']), 12)

    def test_role_distribution_all_sizes(self):
        for count in range(5, 13):
            state = rules.new_game(1)
            for uid in range(count):
                rules.join(state, uid, str(uid))
            rules.start(state, 0)
            roles = [p['role'] for p in state['players'].values()]
            self.assertEqual(roles.count('mafia'), 1 if count == 5 else (2 if count <= 8 else 3))
            self.assertEqual(roles.count('doctor'), 1)
            self.assertEqual(roles.count('commissioner'), 1)
            with self.assertRaises(rules.GameError):
                rules.start(state, 0)

    def test_doctor_protects_even_when_targeted(self):
        state = playing()
        for uid, target in ((1, '3'), (2, '3'), (3, '3')):
            rules.act(state, uid, target, 1, 110)
        self.assertFalse(rules.advance(state, 159))
        self.assertTrue(rules.advance(state, 160))
        self.assertTrue(state['players']['3']['alive'])
        self.assertEqual(state['phase'], 'day')

    def test_night_tie_no_death_and_mafia_win(self):
        state = playing()
        rules.act(state, 1, '5', 1, 110)
        rules.act(state, 2, '6', 1, 110)
        rules.advance(state, 160)
        self.assertTrue(all(p['alive'] for p in state['players'].values()))
        state = playing()
        state['players']['6']['alive'] = False
        rules.act(state, 1, '5', 1, 110)
        rules.advance(state, 160)
        self.assertEqual(state['winner'], 'mafia')
        self.assertIsNone(state['deadline'])

    def test_commissioner_only_one_target(self):
        state = playing()
        self.assertIn('**Мафия**', rules.act(state, 4, '1', 1, 110))
        with self.assertRaises(rules.GameError):
            rules.act(state, 4, '5', 1, 111)
        self.assertEqual(state['players']['4']['checks'], {'1': 'Мафия'})
        self.assertIn('**Мафия**', rules.act(state, 4, '1', 1, 111))

    def test_reject_invalid_actions(self):
        for uid, target, token, now in ((5, '1', 1, 110), (1, '2', 1, 110), (4, '4', 1, 110),
                                         (1, '5', 0, 110), (99, '5', 1, 110), (1, '5', 1, 160)):
            with self.subTest(uid=uid, target=target, token=token, now=now):
                with self.assertRaises(rules.GameError):
                    rules.act(playing(), uid, target, token, now)
        state = playing()
        state['players']['1']['alive'] = False
        with self.assertRaises(rules.GameError):
            rules.act(state, 1, '5', 1, 110)

    def test_vote_tie_skip_and_change(self):
        for votes in ({'1': '5', '2': '6'}, {'1': 'skip', '2': 'skip', '3': '1'}):
            state = playing()
            rules.enter_phase(state, 'vote', 200)
            state['votes'] = votes
            rules.advance(state, 260)
            self.assertTrue(all(p['alive'] for p in state['players'].values()))
        state = playing()
        rules.enter_phase(state, 'vote', 200)
        rules.act(state, 1, '5', state['token'], 201)
        rules.act(state, 1, '6', state['token'], 202)
        self.assertEqual(state['votes'], {'1': '6'})
        with self.assertRaises(rules.GameError):
            rules.act(state, 1, '1', state['token'], 202)

    def test_town_win_and_draw(self):
        state = playing()
        state['players']['2']['alive'] = False
        rules.enter_phase(state, 'vote', 200)
        state['votes'] = {'3': '1', '4': '1'}
        rules.advance(state, 260)
        self.assertEqual(state['winner'], 'town')
        state = playing()
        state['round'] = 20
        rules.enter_phase(state, 'vote', 200)
        rules.advance(state, 260)
        self.assertEqual(state['winner'], 'draw')

    def test_pause_restart_and_stale_menu(self):
        state = playing()
        rules.toggle_pause(state, 110)
        self.assertEqual(state['remaining'], 50)
        self.assertFalse(rules.advance(state, 99999))
        with self.assertRaises(rules.GameError):
            rules.act(state, 1, '5', state['token'], 99999)
        restored = copy.deepcopy(state)
        rules.toggle_pause(restored, 2000)
        self.assertEqual(restored['deadline'], 2050)
        with self.assertRaises(rules.GameError):
            rules.act(restored, 1, '5', 1, 2001)
        rules.advance(restored, 99999)
        self.assertEqual(restored['phase'], 'day')
        self.assertEqual(restored['deadline'], 100119)

    def test_cancel_and_departure(self):
        state = playing()
        rules.act(state, 1, '5', 1, 110)
        rules.depart(state, 5)
        self.assertFalse(state['players']['5']['alive'])
        self.assertEqual(state['actions'], {})
        rules.depart(state, 1)
        rules.depart(state, 2)
        self.assertEqual(state['winner'], 'town')
        state = playing()
        rules.cancel(state)
        self.assertEqual(state['phase'], 'cancelled')
        self.assertIsNone(state['winner'])
        self.assertFalse(rules.advance(state, 9999))

    def test_public_panel_hides_roles_and_actions(self):
        state = playing()
        rules.act(state, 4, '1', 1, 110)
        game = {'id': 1, 'state': state}
        fields = game_embed(game).to_dict()['fields']
        public_players = fields[0]['value']
        self.assertNotIn('Мафия', public_players)
        self.assertNotIn('Комиссар', public_players)
        civilian, _ = private_panel(game, 5)
        self.assertNotIn('Проверки', civilian)
        self.assertNotIn('Команда мафии', civilian)
        commissioner, components = private_panel(game, 4)
        self.assertIn('<@1>: Мафия', commissioner)
        self.assertEqual(components, [])
        rules.finish(state, 'town')
        self.assertIn('Комиссар', game_embed(game).to_dict()['fields'][0]['value'])


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_floor(self):
        bot = SimpleNamespace(database=SimpleNamespace(get_command_access_level=AsyncMock(return_value=1)))
        self.assertEqual(await command_access_level(bot, GUILD_ID, 'mafia create'), 5)
        bot.database.get_command_access_level.assert_not_awaited()

    async def test_control_and_player_permissions(self):
        from unittest.mock import patch
        for action, level in (('start', 4), ('pause', 4), ('stop', 4), ('confirm_stop', 4), ('join', 0)):
            cog = object.__new__(MafiaCog)
            cog.bot = SimpleNamespace()
            cog._game_locks = WeakValueDictionary()
            cog.advance_game = AsyncMock()
            cog.maintain_game = AsyncMock()
            cog.sync_message = AsyncMock()
            member = Mock(spec=disnake.Member)
            member.bot = False
            inter = SimpleNamespace(
                guild_id=GUILD_ID, author=member,
                component=SimpleNamespace(custom_id=f'mafia:{action}:1'),
                id=1, token='test-interaction', _session=None,
                type=disnake.InteractionType.component,
                edit_original_response=AsyncMock(),
            )
            inter.response = disnake.InteractionResponse(inter)
            adapter = SimpleNamespace(create_interaction_response=AsyncMock())
            context = disnake.InteractionResponse.defer.__globals__['async_context']
            context_token = context.set(adapter)
            try:
                with patch('cogs.mafia.member_access_level', AsyncMock(return_value=level)), patch('cogs.mafia.has_family_role', AsyncMock(return_value=False)):
                    await cog.handle_component(inter)
            finally:
                context.reset(context_token)
            response = adapter.create_interaction_response.call_args.kwargs
            self.assertEqual(response['type'], disnake.InteractionResponseType.deferred_channel_message.value)
            self.assertEqual(response['data']['flags'], disnake.MessageFlags.ephemeral.flag)
            cog.advance_game.assert_not_awaited()
            cog.maintain_game.assert_not_awaited()
            cog.sync_message.assert_not_awaited()
            expected = 'семейной ролью' if action == 'join' else 'уровня допуска'
            self.assertIn(expected, inter.edit_original_response.call_args.args[0])

    async def test_component_reply_mode_is_private_for_role_and_action_menus(self):
        for custom_id in ('mafia:role:1', 'mafia:act:1:2', 'mafia:join:1', 'mafia:start:1'):
            cog = object.__new__(MafiaCog)
            cog._game_locks = WeakValueDictionary()
            cog._handle_component = AsyncMock()
            inter = SimpleNamespace(
                guild_id=GUILD_ID, author=Mock(spec=disnake.Member),
                component=SimpleNamespace(custom_id=custom_id),
                response=SimpleNamespace(defer=AsyncMock()),
            )
            await cog.handle_component(inter)
            inter.response.defer.assert_awaited_once_with(with_message=True, ephemeral=True)
            cog._handle_component.assert_awaited_once_with(inter)

    async def test_panel_repair_restores_lobby_buttons_and_clears_error_text(self):
        state = rules.new_game(1)
        rules.join(state, 1, 'Player')
        game = {'id': 1, 'state': state, 'channel_id': 123, 'message_id': 456,
                'revision': 5, 'message_revision': 5}
        message = SimpleNamespace(id=456, edit=AsyncMock())
        channel = Mock(spec=disnake.TextChannel)
        channel.guild = SimpleNamespace(id=GUILD_ID)
        channel.fetch_message = AsyncMock(return_value=message)
        db = SimpleNamespace(get_mafia_game=AsyncMock(return_value=game),
                             set_mafia_message=AsyncMock(), pending_mafia_games=AsyncMock(return_value=[{'id': 1}]))
        cog = object.__new__(MafiaCog)
        cog.bot = SimpleNamespace(database=db, get_channel=lambda _: channel)
        cog._game_locks, cog._message_locks = WeakValueDictionary(), WeakValueDictionary()
        await cog.restore_mafia_panels()
        kwargs = message.edit.call_args.kwargs
        self.assertIsNone(kwargs['content'])
        self.assertEqual([button.custom_id for button in kwargs['components']],
                         ['mafia:join:1', 'mafia:leave:1', 'mafia:start:1', 'mafia:stop:1'])
        self.assertEqual(state['phase'], 'lobby')
        db.set_mafia_message.assert_awaited_once_with(1, 456, 5)


if __name__ == '__main__':
    unittest.main()
