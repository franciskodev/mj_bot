from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import disnake

from cogs.activity import ActivityCog
from config import FAMILY_MEMBER_ROLE_ID, FAMILY_RANK_ROLE_IDS, GUILD_ID, MEDIA_ROLE_ID


class FamilyMemberRoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.famq = SimpleNamespace(id=FAMILY_MEMBER_ROLE_ID)
        self.trigger = SimpleNamespace(id=FAMILY_RANK_ROLE_IDS['young'])
        self.guild = SimpleNamespace(
            id=GUILD_ID, get_role=Mock(return_value=self.famq),
            get_member=Mock(), fetch_member=AsyncMock(),
        )
        self.db = SimpleNamespace(get_user=AsyncMock(return_value={'is_active': True}))
        self.cog = ActivityCog.__new__(ActivityCog)
        self.cog.bot = SimpleNamespace(database=self.db)
        self.cog.automated_role_removals = set()
        self.cog.get_family_role_ids = AsyncMock(return_value=set())
        self.cog.get_family_member_trigger_role_ids = AsyncMock(return_value={self.trigger.id})
        self.cog.sync_member_family = AsyncMock()

    def member(self, roles):
        return SimpleNamespace(
            id=100, guild=self.guild, bot=False, roles=roles, add_roles=AsyncMock(),
            guild_permissions=SimpleNamespace(administrator=True),
        )

    async def test_removing_famq_during_bulk_removal_does_not_restore_it(self):
        before = self.member([self.famq, self.trigger])
        after = self.member([self.trigger])
        self.guild.get_member.return_value = after
        self.guild.fetch_member.return_value = after
        await self.cog.on_member_update(before, after)
        after.add_roles.assert_not_awaited()

    async def test_adding_family_rank_still_assigns_famq(self):
        before = self.member([])
        after = self.member([self.trigger])
        self.guild.get_member.return_value = after
        self.guild.fetch_member.return_value = after
        await self.cog.on_member_update(before, after)
        after.add_roles.assert_awaited_once_with(
            self.famq, reason="Автоматическая роль участника семьи",
        )

    async def test_queued_event_does_not_restore_famq_after_roles_removed(self):
        stale = self.member([self.trigger])
        live = self.member([])
        self.guild.get_member.return_value = stale
        self.guild.fetch_member.return_value = live
        await self.cog.ensure_family_member_role(stale)
        stale.add_roles.assert_not_awaited()
        live.add_roles.assert_not_awaited()

    async def test_database_wait_does_not_use_outdated_role_snapshot(self):
        stale = self.member([self.trigger])
        self.guild.get_member.return_value = self.member([])
        await self.cog.ensure_family_member_role(stale)
        self.guild.fetch_member.assert_not_awaited()
        stale.add_roles.assert_not_awaited()


class MediaActivityExemptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.member = SimpleNamespace(
            id=100, bot=False, roles=[SimpleNamespace(id=MEDIA_ROLE_ID)],
            guild_permissions=SimpleNamespace(administrator=False),
            mention='<@100>', edit=AsyncMock(),
        )
        self.profile = dict(
            discord_id=100, last_message_at=None,
            weekly_voice_seconds=0, daily_voice_seconds=0,
        )
        started = disnake.utils.utcnow() - timedelta(days=8)
        self.db = SimpleNamespace(
            get_activity_enforcement_started_at=AsyncMock(return_value=started),
            get_daily_warning_started_at=AsyncMock(return_value=started),
            get_activity_remove_roles=AsyncMock(return_value=True),
            get_inactive_users=AsyncMock(return_value=[self.profile]),
            get_daily_warning_users=AsyncMock(return_value=[self.profile]),
            get_mafia_saved_access=AsyncMock(return_value=0),
            record_inactivity_notice=AsyncMock(), record_activity_warning=AsyncMock(),
            deactivate_user=AsyncMock(),
        )
        self.guild = SimpleNamespace(
            id=GUILD_ID, unavailable=False,
            get_member=Mock(return_value=self.member),
            fetch_member=AsyncMock(return_value=self.member),
        )
        self.cog = ActivityCog.__new__(ActivityCog)
        self.cog.bot = SimpleNamespace(
            database=self.db, get_guild=Mock(return_value=self.guild),
        )
        self.cog.ensure_voice_tracking_fresh = AsyncMock(return_value=1)
        self.cog.voice_tracking_is_fresh = Mock(return_value=True)
        self.cog.get_family_role_ids = AsyncMock(return_value={FAMILY_RANK_ROLE_IDS['young']})

    async def test_media_skips_inactivity_penalties_and_log_only_mode(self):
        with patch('cogs.activity.send_admin_log', new_callable=AsyncMock) as log:
            for remove_roles in (True, False):
                self.db.get_activity_remove_roles.return_value = remove_roles
                await self.cog.run_inactive_check()
            log.assert_not_awaited()
        self.member.edit.assert_not_awaited()
        self.db.deactivate_user.assert_not_awaited()
        self.db.record_inactivity_notice.assert_not_awaited()

    async def test_media_added_since_cached_snapshot_prevents_role_removal(self):
        cached = SimpleNamespace(**vars(self.member))
        cached.roles = []
        self.guild.get_member.return_value = cached
        await self.cog.run_inactive_check()
        self.guild.fetch_member.assert_awaited_once_with(100)
        self.member.edit.assert_not_awaited()
        self.db.deactivate_user.assert_not_awaited()

    async def test_daily_reminders_resume_only_after_media_removed(self):
        with patch('cogs.activity.send_dm_notification', new_callable=AsyncMock) as notify:
            notify.return_value = SimpleNamespace(delivered=True)
            await self.cog.run_daily_warning_check()
            notify.assert_not_awaited()
            self.db.record_activity_warning.assert_not_awaited()
            self.member.roles = []
            await self.cog.run_daily_warning_check()
            notify.assert_awaited_once()
            self.db.record_activity_warning.assert_awaited_once()
