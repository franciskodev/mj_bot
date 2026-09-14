"""Application migration and isolation checks against a temporary PostgreSQL cluster."""
import getpass
from pathlib import Path
import unittest

import asyncpg

from config import GUILD_ID
from database import Database
import test_mafia_database as postgres_fixture


class ApplicationDatabaseTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(postgres_fixture.DatabaseTests.setUpClass.__func__)
    tearDownClass = classmethod(postgres_fixture.DatabaseTests.tearDownClass.__func__)

    async def asyncSetUp(self):
        self.db = Database()
        self.db.pool = await asyncpg.create_pool(
            host=str(self.root), port=55439, user=getpass.getuser(), database='postgres',
            min_size=1, max_size=2,
        )
        source = (Path(__file__).resolve().parents[1] / 'database.py').read_text()
        self.schema = 'CREATE TABLE IF NOT EXISTS applications (' + source.split(
            'CREATE TABLE IF NOT EXISTS applications (', 1
        )[1].split('CREATE TABLE IF NOT EXISTS application_blocks (', 1)[0]
        panel_schema = 'CREATE TABLE IF NOT EXISTS recruiter_application_config (' + source.split(
            'CREATE TABLE IF NOT EXISTS recruiter_application_config (', 1
        )[1].split('CREATE TABLE IF NOT EXISTS families (', 1)[0]
        await self.db.pool.execute(self.schema + panel_schema)
        await self.db.pool.execute('TRUNCATE applications, recruiter_application_config RESTART IDENTITY')

    async def asyncTearDown(self):
        await self.db.pool.close()

    async def create_application(self, application_type='family'):
        return await self.db.create_application(
            GUILD_ID, 'INTERPOLOV', 'Seattle', 91, 'Applicant', 'Nickname', '123',
            20, 'Motivation', 'https://example.com/photo.png', application_type,
        )

    async def test_types_have_independent_open_requests_and_cooldowns(self):
        family = await self.create_application()
        self.assertTrue(await self.db.has_open_application(91, GUILD_ID))
        self.assertFalse(await self.db.has_open_application(91, GUILD_ID, 'recruiter'))
        self.assertIsNone(await self.db.get_application_cooldown_until(GUILD_ID, 91, 'recruiter'))
        recruiter = await self.create_application('recruiter')
        self.assertEqual((await self.db.get_application(recruiter['id']))['application_type'], 'recruiter')
        self.assertTrue(await self.db.has_open_application(91, GUILD_ID, 'recruiter'))
        await self.db.review_application(recruiter['id'], 'accepted', 92)
        self.assertFalse(await self.db.has_open_application(91, GUILD_ID, 'recruiter'))
        self.assertTrue(await self.db.has_open_application(91, GUILD_ID))
        self.assertIsNotNone(await self.db.get_application_cooldown_until(GUILD_ID, 91, 'recruiter'))
        self.assertEqual((await self.db.get_application(family['id']))['status'], 'pending')
        await self.db.reset_application_cooldown(GUILD_ID, 91)
        for kind in ('family', 'recruiter'):
            self.assertIsNone(await self.db.get_application_cooldown_until(GUILD_ID, 91, kind))

    async def test_migration_preserves_existing_family_applications_and_is_repeatable(self):
        family = await self.create_application()
        await self.db.pool.execute('ALTER TABLE applications DROP COLUMN application_type')
        await self.db.pool.execute(self.schema)
        await self.db.pool.execute(self.schema)
        restored = await self.db.get_application(family['id'])
        self.assertEqual(restored['application_type'], 'family')
        self.assertEqual(restored['game_nickname'], family['game_nickname'])
        self.assertTrue(await self.db.has_open_application(91, GUILD_ID))

    async def test_recruiter_panel_settings_survive_reload_and_can_be_updated(self):
        self.assertIsNone(await self.db.get_recruiter_application_config(GUILD_ID))
        await self.db.save_recruiter_application_config(GUILD_ID, 100, 200, 300)
        await self.db.save_recruiter_application_config(GUILD_ID, 101, 201, 301)
        config = await self.db.get_recruiter_application_config(GUILD_ID)
        self.assertEqual(dict(config), dict(
            guild_id=GUILD_ID, panel_channel_id=101, category_id=201, panel_message_id=301,
        ))
        self.assertIsNone(await self.db.get_recruiter_application_config(123))
