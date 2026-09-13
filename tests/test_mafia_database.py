"""Integration tests use an isolated temporary PostgreSQL cluster, never .env."""
import asyncio
import getpass
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import asyncpg

from cogs import mafia_game as rules
from database import Database
from config import MAFIA_CLEANUP_DELAY_SECONDS


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        binary = shutil.which('initdb')
        if binary is None:
            candidates = sorted(Path('/usr/lib/postgresql').glob('*/bin/initdb'))
            binary = str(candidates[-1]) if candidates else None
        if binary is None:
            raise unittest.SkipTest('PostgreSQL server binaries are not installed')
        cls.pg_ctl = str(Path(binary).with_name('pg_ctl'))
        cls.temp = tempfile.TemporaryDirectory(prefix='mafia-test-')
        cls.root = Path(cls.temp.name)
        cls.data = cls.root / 'data'
        try:
            subprocess.run([binary, '-D', str(cls.data), '-A', 'trust', '--no-locale', '-E', 'UTF8'],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([cls.pg_ctl, '-D', str(cls.data), '-l', str(cls.root / 'postgres.log'),
                            '-o', f"-h '' -k {cls.root} -p 55439", '-w', 'start'],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            cls.temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            subprocess.run([cls.pg_ctl, '-D', str(cls.data), '-m', 'fast', '-w', 'stop'],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            cls.temp.cleanup()

    async def asyncSetUp(self):
        self.db = Database()
        self.db.pool = await asyncpg.create_pool(host=str(self.root), port=55439,
                                                 user=getpass.getuser(), database='postgres', min_size=1, max_size=5)
        source = (Path(__file__).resolve().parents[1] / 'database.py').read_text()
        schema = source.split('CREATE TABLE IF NOT EXISTS mafia_games (', 1)[1].split('CREATE TABLE IF NOT EXISTS giveaways (', 1)[0]
        await self.db.pool.execute('CREATE TABLE IF NOT EXISTS mafia_games (' + schema)
        await self.db.pool.execute('TRUNCATE mafia_notifications,mafia_events,mafia_role_holds,mafia_results,mafia_games RESTART IDENTITY')

    async def asyncTearDown(self):
        await self.db.pool.close()

    async def create_started_game(self, guild=1):
        state = rules.new_game(1)
        for uid in range(1, 7):
            rules.join(state, uid, str(uid))
        rules.start(state, 100)
        for uid, role in zip(state['players'], ('mafia', 'mafia', 'doctor', 'commissioner', 'civilian', 'civilian')):
            state['players'][uid]['role'] = role
        return await self.db.create_mafia_game(guild, 123, state)

    async def finish_game(self, game_id, winner='town', guild=1):
        async with self.db.mafia_transaction(guild, game_id) as game:
            rules.finish(game['state'], winner)

    async def test_only_one_active_game(self):
        outcomes = await asyncio.gather(*(self.db.create_mafia_game(1, 123, rules.new_game(uid)) for uid in range(5)), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, int) for result in outcomes), 1)
        self.assertEqual(sum(isinstance(result, asyncpg.UniqueViolationError) for result in outcomes), 4)
        self.assertIsNone(await self.db.get_mafia_game(2))

    async def test_simultaneous_joins_and_capacity(self):
        game_id = await self.db.create_mafia_game(1, 123, rules.new_game(1))
        async def join(uid):
            async with self.db.mafia_transaction(1, game_id) as game:
                rules.join(game['state'], uid, str(uid))
                await asyncio.sleep(0.001)
        outcomes = await asyncio.gather(*(join(uid) for uid in range(13)), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, rules.GameError) for result in outcomes), 1)
        game = await self.db.get_mafia_game(1, game_id)
        self.assertEqual(len(game['state']['players']), 12)
        self.assertEqual(game['revision'], 13)

    async def test_state_rollback_and_restart(self):
        game_id = await self.create_started_game()
        async with self.db.mafia_transaction(1, game_id) as game:
            rules.act(game['state'], 4, '1', 1, 110)
            rules.toggle_pause(game['state'], 111)
        try:
            async with self.db.mafia_transaction(1, game_id) as game:
                rules.cancel(game['state'])
                raise RuntimeError('rollback')
        except RuntimeError:
            pass
        reopened = Database()
        reopened.pool = self.db.pool
        game = await reopened.get_mafia_game(1, game_id)
        self.assertTrue(game['state']['paused'])
        self.assertEqual(game['state']['players']['4']['checks'], {'1': 'Мафия'})
        self.assertEqual(game['state']['remaining'], 49)
        self.assertTrue(game['active'])
        self.assertEqual(await self.db.pool.fetchval('SELECT COUNT(*) FROM mafia_results'), 0)

    async def test_finishing_is_atomic_and_idempotent(self):
        game_id = await self.create_started_game()
        async def finish():
            async with self.db.mafia_transaction(1, game_id) as game:
                if game['state']['phase'] != 'finished':
                    rules.finish(game['state'], 'town')
        await asyncio.gather(*(finish() for _ in range(4)))
        self.assertEqual(await self.db.pool.fetchval('SELECT COUNT(*) FROM mafia_results'), 6)
        self.assertEqual(await self.db.pool.fetchval('SELECT COUNT(*) FROM mafia_results WHERE won'), 4)
        game = await self.db.get_mafia_game(1, game_id)
        self.assertFalse(game['active'])
        results = await self.db.mafia_leaderboard(1)
        self.assertEqual(results[0]['points'], 3)
        self.assertEqual(results[-1]['points'], 1)
        await self.db.set_mafia_message(game_id, 999, game['revision'])
        for event in await self.db.get_mafia_events(game_id):
            await self.db.mark_mafia_event_sent(event['id'], 1000 + event['id'])
        self.assertEqual(await self.db.pending_mafia_games(1), [])
        await self.create_started_game()

    async def test_cancel_does_not_score_and_finalization_rollback(self):
        game_id = await self.create_started_game()
        with self.assertRaises(RuntimeError):
            async with self.db.mafia_transaction(1, game_id) as game:
                rules.finish(game['state'], 'town')
                raise RuntimeError('rollback')
        self.assertEqual(await self.db.pool.fetchval('SELECT COUNT(*) FROM mafia_results'), 0)
        async with self.db.mafia_transaction(1, game_id) as game:
            rules.cancel(game['state'])
        self.assertEqual(await self.db.mafia_leaderboard(1), [])

    async def test_leaderboards_and_time_filters(self):
        for _ in range(5):
            await self.finish_game(await self.create_started_game())
        for metric in ('points', 'wins', 'games', 'win_rate', 'mafia_wins', 'town_wins', 'survived'):
            rows = await self.db.mafia_leaderboard(1, metric)
            self.assertEqual(len(rows), 6)
        self.assertEqual((await self.db.mafia_leaderboard(1, 'win_rate'))[0]['win_rate'], 100)
        stats = await self.db.mafia_leaderboard(1, user_id=1)
        self.assertEqual(stats[0]['games'], 5)
        self.assertEqual(stats[0]['wins'], 0)
        self.assertEqual(stats[0]['points'], 5)
        self.assertEqual(await self.db.mafia_leaderboard(2), [])
        await self.db.pool.execute("UPDATE mafia_results SET finished_at=NOW()-INTERVAL '8 days' WHERE game_id=1")
        self.assertEqual(await self.db.mafia_leaderboard(1, 'win_rate', 7), [])
        self.assertEqual((await self.db.mafia_leaderboard(1, 'games', 7))[0]['games'], 4)
        self.assertEqual((await self.db.mafia_leaderboard(1, 'games', 30))[0]['games'], 5)
        with self.assertRaises(ValueError):
            await self.db.mafia_leaderboard(1, 'invalid')

    async def test_role_hold_survives_restart_and_game_end(self):
        game_id = await self.create_started_game()
        await self.db.hold_mafia_roles(game_id, 1, 1, [100, 200], 5)
        await self.db.hold_mafia_roles(game_id, 1, 1, [200, 300], 4)
        reopened = Database()
        reopened.pool = self.db.pool
        self.assertEqual(await reopened.get_mafia_saved_access(1, 1), 5)
        self.assertEqual(await reopened.get_mafia_saved_access(2, 1), 0)
        holds = await reopened.get_mafia_role_holds(game_id)
        self.assertEqual(set(holds[0]['role_ids']), {100, 200, 300})
        await self.finish_game(game_id)
        self.assertEqual(await reopened.get_mafia_saved_access(1, 1), 5)
        self.assertTrue(await reopened.has_other_mafia_role_hold(1, 1, game_id + 1))
        self.assertFalse(await reopened.has_other_mafia_role_hold(1, 1, game_id))
        await reopened.finish_mafia_role_hold(game_id, 1)
        self.assertEqual(await reopened.get_mafia_saved_access(1, 1), 0)
        self.assertEqual(await reopened.get_mafia_role_holds(game_id), [])

    async def test_rooms_and_public_events_are_durable(self):
        game_id = await self.create_started_game()
        async with self.db.mafia_transaction(1, game_id) as game:
            rules.act(game['state'], 4, '1', 1, 110)
        self.assertEqual(await self.db.get_mafia_events(game_id), [])
        async with self.db.mafia_transaction(1, game_id) as game:
            game['state']['rooms'] = {'events': 12345, 'mafia_text': 12346}
        game = await self.db.get_mafia_game(1, game_id)
        self.assertEqual(game['channel_id'], 12345)
        async with self.db.mafia_transaction(1, game_id) as game:
            rules.advance(game['state'], 160)
        events = await self.db.get_mafia_events(game_id)
        self.assertEqual(len(events), 1)
        self.assertNotIn('checks', events[0]['payload'])
        self.assertNotIn('players', events[0]['payload'])
        await self.db.mark_mafia_event_sent(events[0]['id'], 222)
        self.assertEqual(await self.db.get_mafia_events(game_id), [])

    async def test_personal_notifications_commit_with_action_and_survive_restart(self):
        game_id = await self.create_started_game()
        with self.assertRaises(RuntimeError):
            async with self.db.mafia_transaction(1, game_id) as game:
                rules.act(game['state'], 4, '1', 1, 110)
                raise RuntimeError('rollback')
        self.assertEqual(await self.db.get_mafia_notifications(1), [])
        async with self.db.mafia_transaction(1, game_id) as game:
            rules.act(game['state'], 4, '1', 1, 110)
        reopened = Database()
        reopened.pool = self.db.pool
        rows = await reopened.get_mafia_notifications(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['discord_id'], 4)
        self.assertIn('Мафия', rows[0]['payload'])
        self.assertEqual(await reopened.get_mafia_notifications(2), [])
        self.assertEqual(await reopened.get_mafia_events(game_id), [])
        await reopened.retry_mafia_notification(rows[0]['id'])
        self.assertEqual(await reopened.get_mafia_notifications(1), [])
        await self.db.pool.execute('UPDATE mafia_notifications SET next_attempt_at=NOW()')
        self.assertEqual(len(await reopened.get_mafia_notifications(1)), 1)
        await reopened.finish_mafia_notification(rows[0]['id'], 55)
        async with reopened.mafia_transaction(1, game_id) as game:
            game['state']['rooms'] = {'events': 123}
        self.assertEqual(await reopened.get_mafia_notifications(1), [])

    async def test_readiness_is_durable_and_concurrent_last_confirmations_start_once(self):
        state = rules.new_game(1)
        for uid in range(1, 6):
            rules.join(state, uid, str(uid))
        game_id = await self.db.create_mafia_game(1, 123, state)
        async with self.db.mafia_transaction(1, game_id) as game:
            rules.request_start(game['state'])
        game = await self.db.get_mafia_game(1, game_id)
        token = game['state']['token']
        self.assertIsNone(game['state']['deadline'])
        self.assertEqual(len(await self.db.get_mafia_notifications(1)), 5)
        async with self.db.mafia_transaction(1, game_id) as game:
            for uid in range(1, 5):
                rules.confirm_start(game['state'], uid, token)
        reopened = Database()
        reopened.pool = self.db.pool
        self.assertEqual(len((await reopened.get_mafia_game(1, game_id))['state']['confirmations']), 4)

        async def confirm():
            async with reopened.mafia_transaction(1, game_id) as game:
                if rules.confirm_start(game['state'], 5, token):
                    rules.start(game['state'], 100)
                    game['state'].update(setup_pending=True, paused=True, deadline=None)

        outcomes = await asyncio.gather(confirm(), confirm(), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, rules.GameError) for result in outcomes), 1)
        game = await reopened.get_mafia_game(1, game_id)
        self.assertEqual(game['state']['phase'], 'night')
        self.assertTrue(game['state']['setup_pending'])
        self.assertEqual(sum(p['role'] == 'mafia' for p in game['state']['players'].values()), 1)
        self.assertEqual(await self.db.pool.fetchval("SELECT count(*) FROM mafia_notifications WHERE payload->>'kind'='role'"), 0)

    async def test_existing_role_hold_schema_upgrades_to_level_six(self):
        game_id = await self.create_started_game()
        await self.db.hold_mafia_roles(game_id, 1, 1, [100], 5)
        await self.db.pool.execute('''
            ALTER TABLE mafia_role_holds DROP CONSTRAINT mafia_role_holds_access_level_check;
            ALTER TABLE mafia_role_holds ADD CONSTRAINT mafia_role_holds_access_level_check
                CHECK(access_level BETWEEN 0 AND 5);
        ''')
        source = (Path(__file__).resolve().parents[1] / 'database.py').read_text()
        migration = 'ALTER TABLE mafia_role_holds' + source.split('ALTER TABLE mafia_role_holds', 1)[1].split('CREATE TABLE IF NOT EXISTS mafia_events', 1)[0]
        await self.db.pool.execute(migration)
        await self.db.hold_mafia_roles(game_id, 1, 2, [200], 6)
        self.assertEqual(await self.db.get_mafia_saved_access(1, 1), 5)
        self.assertEqual(await self.db.get_mafia_saved_access(1, 2), 6)
        await self.db.pool.execute(migration)
        self.assertEqual(await self.db.get_mafia_saved_access(1, 2), 6)

    async def test_cleanup_schedule_and_pending_recovery_survive_restart(self):
        for phase in ('finished', 'cancelled'):
            game_id = await self.create_started_game()
            before = await self.db.pool.fetchval('SELECT EXTRACT(EPOCH FROM NOW())::DOUBLE PRECISION')
            async with self.db.mafia_transaction(1, game_id) as game:
                game['state']['rooms_enabled'] = True
                if phase == 'finished':
                    rules.finish(game['state'], 'town')
                else:
                    rules.cancel(game['state'])
            reopened = Database()
            reopened.pool = self.db.pool
            game = await reopened.get_mafia_game(1, game_id)
            deadline = game['state']['cleanup_at']
            self.assertGreaterEqual(deadline, before + MAFIA_CLEANUP_DELAY_SECONDS)
            self.assertLess(deadline, before + MAFIA_CLEANUP_DELAY_SECONDS + 5)
            await reopened.set_mafia_message(game_id, 999, game['revision'])
            for event in await reopened.get_mafia_events(game_id):
                await reopened.mark_mafia_event_sent(event['id'], 1000 + event['id'])
            self.assertIn(game_id, [r['id'] for r in await reopened.pending_mafia_games(1)])
            await reopened.hold_mafia_roles(game_id, 1, 1, [100], 5)
            async with reopened.mafia_transaction(1, game_id) as game:
                game['state']['rooms_deleted'] = True
            self.assertEqual((await reopened.get_mafia_game(1, game_id))['state']['cleanup_at'], deadline)
            self.assertIn(game_id, [r['id'] for r in await reopened.pending_mafia_games(1)])
            await reopened.finish_mafia_role_hold(game_id, 1)
            self.assertNotIn(game_id, [r['id'] for r in await reopened.pending_mafia_games(1)])
        self.assertEqual(await self.db.pool.fetchval('SELECT COUNT(*) FROM mafia_results'), 6)

    async def test_application_roles_migration_isolation_and_replacement(self):
        from unittest.mock import patch
        from config import FAMILY_NAME, GUILD_ID

        configured = Database()
        dsn = f"postgresql:///postgres?host={self.root}&port=55439&user={getpass.getuser()}"
        with patch('database.DATABASE_URL', dsn):
            await configured.connect()
            try:
                servers = {row['name']: row for row in await configured.get_game_servers(GUILD_ID)}
                seattle, orlando = servers['Seattle']['id'], servers['Orlando']['id']
                await configured.set_application_staff_roles(GUILD_ID, seattle, 101, 102)
                await configured.set_application_staff_roles(GUILD_ID, orlando, 201, 202)
                self.assertEqual(await configured.get_highest_role_access_level(GUILD_ID, {101}), 2)
                self.assertEqual(await configured.get_highest_role_access_level(GUILD_ID, {102}), 4)
                self.assertEqual(await configured.get_highest_role_access_level(999, {102}), 0)
                with self.assertRaises(asyncpg.UniqueViolationError):
                    await configured.set_application_staff_roles(GUILD_ID, orlando, 301, 102)
                rows = await configured.get_application_staff_roles(GUILD_ID)
                self.assertEqual({r['role_id'] for r in rows if r['server_id'] == orlando}, {201, 202})
                await configured.set_application_staff_roles(GUILD_ID, seattle, 103, 104)
                self.assertEqual(await configured.get_highest_role_access_level(GUILD_ID, {101, 102}), 0)
                self.assertEqual(await configured.get_highest_role_access_level(GUILD_ID, {104}), 4)
                await configured.remove_deleted_role_references(GUILD_ID, 103)
                self.assertEqual(await configured.get_highest_role_access_level(GUILD_ID, {103}), 0)
                await configured.delete_game_server(GUILD_ID, 'Orlando')
                self.assertEqual(await configured.get_highest_role_access_level(GUILD_ID, {201, 202}), 0)
                await configured.pool.execute(
                    "UPDATE families SET name='Legacy family' WHERE guild_id=$1 AND active=TRUE", GUILD_ID)
                application = await configured.create_application(
                    GUILD_ID, 'Legacy family', 'Seattle', 1, 'candidate', 'nickname', '123', 20, 'goal', 'https://example.com')
            finally:
                await configured.close()
            # Repeated startup migrates old family data without losing role settings.
            await configured.connect()
            try:
                self.assertEqual([r['name'] for r in await configured.get_families(GUILD_ID)], [FAMILY_NAME])
                self.assertEqual((await configured.get_application(application['id']))['family_name'], FAMILY_NAME)
                self.assertEqual(await configured.get_highest_role_access_level(GUILD_ID, {104}), 4)
            finally:
                await configured.close()

    async def test_security_role_history_persists_with_time_and_guild_isolation(self):
        from datetime import timedelta

        source = (Path(__file__).resolve().parents[1] / 'database.py').read_text()
        schema = source.split('CREATE TABLE IF NOT EXISTS security_role_removals (', 1)[1].split(
            'CREATE TABLE IF NOT EXISTS command_access_levels (', 1)[0]
        await self.db.pool.execute('CREATE TABLE IF NOT EXISTS security_role_removals (' + schema)
        await self.db.pool.execute('TRUNCATE security_role_removals')
        now = await self.db.pool.fetchval('SELECT NOW()')
        recent = now - timedelta(hours=1)
        await self.db.record_security_role_removals(1, 2, [10, 11], recent)
        await self.db.record_security_role_removals(1, 2, [10], recent)
        await self.db.record_security_role_removals(1, 2, [10], now)
        await self.db.record_security_role_removals(1, 2, [12], now - timedelta(hours=4))
        await self.db.record_security_role_removals(2, 2, [13], recent)
        await self.db.record_security_role_removals(1, 3, [14], recent)
        reopened = Database()
        reopened.pool = await asyncpg.create_pool(
            host=str(self.root), port=55439, user=getpass.getuser(), database='postgres', min_size=1, max_size=1)
        try:
            records = await reopened.get_security_role_removals(1, 2, now - timedelta(hours=3))
            self.assertEqual({(r['role_id'], r['removed_at']) for r in records}, {(10, recent), (11, recent), (10, now)})
            self.assertEqual(len(records), 3)
        finally:
            await reopened.pool.close()

    async def test_protected_log_message_index_scope_duplicates_and_retention(self):
        source = (Path(__file__).resolve().parents[1] / 'database.py').read_text()
        schema = source.split('CREATE TABLE IF NOT EXISTS protected_log_messages (', 1)[1].split(
            'CREATE TABLE IF NOT EXISTS security_role_removals (', 1)[0]
        await self.db.pool.execute('CREATE TABLE IF NOT EXISTS protected_log_messages (' + schema)
        await self.db.pool.execute('TRUNCATE protected_log_messages')
        await self.db.record_protected_log_messages(1, 2, [41, 42, 43])
        await self.db.record_protected_log_messages(1, 2, [42])
        await self.db.record_protected_log_messages(2, 2, [44])
        await self.db.record_protected_log_messages(1, 3, [45])
        reopened = Database()
        reopened.pool = self.db.pool
        self.assertEqual(await reopened.get_protected_log_message_ids(1, 2, {41, 42, 44, 45}), {41, 42})
        self.assertEqual(await reopened.get_protected_log_message_ids(1, 2, set()), set())
        await self.db.prune_protected_log_messages(42)
        self.assertEqual(await reopened.get_protected_log_message_ids(1, 2, {41, 42, 43}), {42, 43})


if __name__ == '__main__':
    unittest.main()
