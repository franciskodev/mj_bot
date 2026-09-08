import copy
import unittest
from weakref import WeakValueDictionary
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import disnake
from access_control import command_access_level
from cogs import mafia_game as rules
from cogs.mafia import game_embed, private_panel, MafiaCog
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
        for count in range(6, 13):
            state = rules.new_game(1)
            for uid in range(count):
                rules.join(state, uid, str(uid))
            rules.start(state, 0)
            roles = [p['role'] for p in state['players'].values()]
            self.assertEqual(roles.count('mafia'), 2 if count <= 8 else 3)
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
            member = Mock(spec=disnake.Member)
            member.bot = False
            inter = SimpleNamespace(
                guild_id=GUILD_ID, author=member,
                component=SimpleNamespace(custom_id=f'mafia:{action}:1'),
                response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
            )
            with patch('cogs.mafia.member_access_level', AsyncMock(return_value=level)), patch('cogs.mafia.has_family_role', AsyncMock(return_value=False)):
                await cog.handle_component(inter)
            cog.advance_game.assert_not_awaited()
            expected = 'семейной ролью' if action == 'join' else 'уровня допуска'
            self.assertIn(expected, inter.edit_original_response.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
