"""Security timer persistence checks against a temporary PostgreSQL cluster."""
import getpass
from datetime import timedelta
from pathlib import Path
import unittest

import asyncpg
import disnake

from config import GUILD_ID
from database import Database
import test_mafia_database as postgres_fixture


class SecurityDatabaseTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(postgres_fixture.DatabaseTests.setUpClass.__func__)
    tearDownClass = classmethod(postgres_fixture.DatabaseTests.tearDownClass.__func__)

    async def asyncSetUp(self):
        self.db = Database()
        self.db.pool = await asyncpg.create_pool(
            host=str(self.root), port=55439, user=getpass.getuser(), database='postgres',
            min_size=1, max_size=2,
        )
        source = (Path(__file__).resolve().parents[1] / 'database.py').read_text()
        schema = 'CREATE TABLE IF NOT EXISTS nickname_warnings (' + source.split(
            'CREATE TABLE IF NOT EXISTS nickname_warnings (', 1
        )[1].split('CREATE TABLE IF NOT EXISTS command_access_levels (', 1)[0]
        await self.db.pool.execute(schema)
        await self.db.pool.execute('TRUNCATE security_role_removals, nickname_warnings')

    async def asyncTearDown(self):
        await self.db.pool.close()

    async def test_reset_is_persistent_scoped_and_allows_new_timer(self):
        now = disnake.utils.utcnow()
        since = now - timedelta(hours=4)
        for guild_id, member_id in ((GUILD_ID, 10), (GUILD_ID, 11), (123, 10)):
            await self.db.record_security_role_removals(guild_id, member_id, [100, 101], now)
        await self.db.clear_security_role_removals(GUILD_ID, 10)
        await self.db.clear_security_role_removals(GUILD_ID, 10)

        # Read from a new connection to verify that the deletion was committed.
        fresh = Database()
        fresh.pool = await asyncpg.create_pool(
            host=str(self.root), port=55439, user=getpass.getuser(), database='postgres',
            min_size=1, max_size=1,
        )
        try:
            self.assertEqual(await fresh.get_security_role_removals(GUILD_ID, 10, since), [])
            for guild_id, member_id in ((GUILD_ID, 11), (123, 10)):
                self.assertEqual(len(await fresh.get_security_role_removals(guild_id, member_id, since)), 2)
            await fresh.record_security_role_removals(GUILD_ID, 10, [102], now)
            rows = await fresh.get_security_role_removals(GUILD_ID, 10, since)
            self.assertEqual([row['role_id'] for row in rows], [102])
        finally:
            await fresh.pool.close()

    async def test_nickname_deadline_survives_renames_and_reload(self):
        deadline = disnake.utils.utcnow() - timedelta(seconds=1)
        await self.db.save_nickname_warning(GUILD_ID, 10, 'Bad', deadline)
        await self.db.save_nickname_warning(GUILD_ID, 10, 'Still bad', deadline + timedelta(days=1))
        fresh = Database()
        fresh.pool = self.db.pool
        row = await fresh.get_nickname_warning(GUILD_ID, 10)
        self.assertEqual(row['deadline'], deadline)
        self.assertEqual(row['nickname'], 'Still bad')
        self.assertEqual(len(await fresh.get_due_nickname_warnings(GUILD_ID)), 1)
        await fresh.mark_nickname_penalty(GUILD_ID, 10)
        self.assertEqual(await fresh.get_due_nickname_warnings(GUILD_ID), [])
        await fresh.save_nickname_warning(123, 10, 'Other guild', deadline)
        await fresh.clear_nickname_warning(GUILD_ID, 10)
        self.assertIsNone(await fresh.get_nickname_warning(GUILD_ID, 10))
        self.assertIsNotNone(await fresh.get_nickname_warning(123, 10))
        new_deadline = deadline + timedelta(days=1)
        await fresh.save_nickname_warning(GUILD_ID, 10, 'New violation', new_deadline)
        row = await fresh.get_nickname_warning(GUILD_ID, 10)
        self.assertEqual(row['deadline'], new_deadline)
        self.assertIsNone(row['penalized_at'])
