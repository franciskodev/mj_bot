import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import disnake

from access_control import promotion_approval_error
from cogs.promotions import PromotionChoices, PromotionsCog, promotion_embed
from config import (
    DEVELOPER_USER_ID, FAMILY_RANK_ROLE_IDS, GUILD_ID, HIGH_FAMQ_ROLE_ID,
    PROMOTION_LOG_CHANNEL_ID, RECRUITMENT_POSITION_ROLE_IDS,
)


class Role:
    def __init__(self, role_id, position):
        self.id, self.position, self.managed = role_id, position, False

    def __ge__(self, other):
        return self.position >= other.position

    def is_default(self):
        return self.id == GUILD_ID


class PromotionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.roles = {role_id: Role(role_id, index + 1)
                      for index, role_id in enumerate(FAMILY_RANK_ROLE_IDS.values())}
        self.roles[GUILD_ID] = Role(GUILD_ID, 0)
        self.roles[500] = Role(500, 100)
        self.roles[600] = Role(600, 1)
        self.roles[700] = Role(700, 4)
        self.roles[RECRUITMENT_POSITION_ROLE_IDS["Chief Recruit"]] = Role(
            RECRUITMENT_POSITION_ROLE_IDS["Chief Recruit"], 2,
        )
        self.guild = SimpleNamespace(id=GUILD_ID, get_role=self.roles.get)
        self.applicant = self.member(10, [FAMILY_RANK_ROLE_IDS["old"], 600,
                                        RECRUITMENT_POSITION_ROLE_IDS["Chief Recruit"]])
        self.reviewer = self.member(20, [FAMILY_RANK_ROLE_IDS["leader"]])
        self.bot_member = self.member(30, [500])
        self.bot_member.guild_permissions = disnake.Permissions(manage_roles=True)
        self.members = {member.id: member for member in (self.applicant, self.reviewer, self.bot_member)}
        self.guild.fetch_member = AsyncMock(side_effect=lambda uid: self.members[uid])
        self.request = {
            "id": 1, "guild_id": GUILD_ID, "discord_id": 10,
            "from_rank": "old", "target_rank": "tech", "server_name": "Seattle",
            "achievements": "Worked", "evidence": "", "status": "pending",
            "log_message_id": 123, "role_change_started": False, "reviewed_by": None,
            "created_at": disnake.utils.utcnow(),
        }
        lock = asyncio.Lock()

        @asynccontextmanager
        async def review_lock(request_id):
            async with lock:
                yield "connection"

        async def begin(*args):
            self.request["role_change_started"] = True

        async def finish(connection, request_id, status, reviewer_id):
            self.request.update(status=status, reviewed_by=reviewer_id)
            return self.request.copy()

        async def edit_member(*, roles, reason):
            self.applicant.roles = [self.roles[GUILD_ID], *roles]
            self.applicant.top_role = max(roles, key=lambda role: role.position)
            return self.applicant

        self.applicant.edit = AsyncMock(side_effect=edit_member)
        self.database = SimpleNamespace(
            get_user=AsyncMock(return_value={"is_active": True, "server_name": "Seattle"}),
            get_game_servers=AsyncMock(return_value=[{"id": 1, "name": "Seattle"}]),
            create_promotion_request=AsyncMock(side_effect=lambda *args: self.request.copy()),
            set_promotion_log_message=AsyncMock(),
            cancel_unpublished_promotion=AsyncMock(),
            get_families=AsyncMock(return_value=[]),
            get_mafia_saved_access=AsyncMock(return_value=0),
            promotion_review_lock=review_lock,
            get_promotion_request=AsyncMock(side_effect=lambda *args: self.request.copy()),
            start_promotion_role_change=AsyncMock(side_effect=begin),
            finish_promotion_request=AsyncMock(side_effect=finish),
            get_application_staff_roles=AsyncMock(return_value=[
                {"server_name": "Seattle", "role_id": 600, "access_level": 2},
                {"server_name": "Seattle", "role_id": 700, "access_level": 4},
                {"server_name": "Orlando", "role_id": 601, "access_level": 2},
            ]),
            get_department_configs=AsyncMock(return_value=[]),
        )
        self.bot = SimpleNamespace(database=self.database, user=SimpleNamespace(id=30))
        self.cog = PromotionsCog(self.bot)
        self.inter = SimpleNamespace(
            guild=self.guild, guild_id=GUILD_ID, author=self.reviewer,
            channel_id=PROMOTION_LOG_CHANNEL_ID,
            component=SimpleNamespace(custom_id="promotion:approved:1"),
            message=SimpleNamespace(id=123, edit=AsyncMock()),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        for path in ("access_control.member_access_level", "cogs.promotions.member_access_level"):
            patcher = patch(path, AsyncMock(return_value=4))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("cogs.promotions.send_admin_log", AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def member(self, uid, role_ids):
        member = Mock(spec=disnake.Member)
        member.id, member.guild, member.bot = uid, self.guild, False
        member.roles = [self.roles[GUILD_ID], *(self.roles[rid] for rid in role_ids)]
        member.top_role = max(member.roles, key=lambda role: role.position)
        member.guild_permissions = disnake.Permissions.none()
        member.mention = f"<@{uid}>"
        return member

    async def test_self_equal_and_higher_rank_cannot_be_approved(self):
        for target, reviewer in (("tech", self.applicant), ("leader", self.reviewer), ("owner", self.reviewer)):
            with self.subTest(target=target, reviewer=reviewer.id):
                self.assertIsNotNone(await promotion_approval_error(self.bot, reviewer, self.applicant, target))
        self.assertIsNone(await promotion_approval_error(self.bot, self.reviewer, self.applicant, "tech"))

    async def test_self_approval_does_not_change_roles(self):
        self.inter.author = self.applicant
        await self.cog.review_button(self.inter)
        self.applicant.edit.assert_not_awaited()
        self.database.finish_promotion_request.assert_not_awaited()

    async def test_discord_hierarchy_and_developer_exemption(self):
        self.roles[FAMILY_RANK_ROLE_IDS["tech"]].position = 90
        self.assertIsNotNone(await promotion_approval_error(self.bot, self.reviewer, self.applicant, "tech"))
        self.roles[FAMILY_RANK_ROLE_IDS["tech"]].position = 6
        self.applicant.id = DEVELOPER_USER_ID
        self.assertIsNotNone(await promotion_approval_error(self.bot, self.reviewer, self.applicant, "tech"))

    async def test_level_below_four_is_rejected(self):
        with patch("cogs.promotions.member_access_level", AsyncMock(return_value=3)):
            await self.cog.review_button(self.inter)
        self.applicant.edit.assert_not_awaited()
        self.database.finish_promotion_request.assert_not_awaited()

    async def test_approval_preserves_positions_and_other_roles(self):
        await self.cog.review_button(self.inter)
        roles = {role.id for role in self.applicant.edit.call_args.kwargs["roles"]}
        self.assertEqual(roles, {FAMILY_RANK_ROLE_IDS["tech"], 600, RECRUITMENT_POSITION_ROLE_IDS["Chief Recruit"]})
        self.assertEqual(self.request["status"], "approved")

    async def test_double_click_changes_roles_once(self):
        await asyncio.gather(self.cog.review_button(self.inter), self.cog.review_button(self.inter))
        self.applicant.edit.assert_awaited_once()
        self.database.finish_promotion_request.assert_awaited_once()

    async def test_discord_failure_leaves_request_pending(self):
        self.applicant.edit.side_effect = disnake.DiscordException("denied")
        with self.assertLogs(level="ERROR"):
            await self.cog.review_button(self.inter)
        self.assertEqual(self.request["status"], "pending")
        self.database.finish_promotion_request.assert_not_awaited()

    async def test_retry_after_role_applied_does_not_assign_twice(self):
        edit = self.applicant.edit.side_effect

        async def timeout_after_edit(**kwargs):
            await edit(**kwargs)
            raise OSError("response lost")

        self.applicant.edit.side_effect = timeout_after_edit
        with self.assertLogs(level="ERROR"):
            await self.cog.review_button(self.inter)
        self.assertEqual(self.request["status"], "pending")
        self.applicant.edit.side_effect = edit
        await self.cog.review_button(self.inter)
        self.applicant.edit.assert_awaited_once()
        self.assertEqual(self.request["status"], "approved")

    async def test_decline_does_not_change_roles(self):
        self.inter.component.custom_id = "promotion:declined:1"
        await self.cog.review_button(self.inter)
        self.applicant.edit.assert_not_awaited()
        self.assertEqual(self.request["status"], "declined")

    async def test_wrong_guild_channel_or_message_cannot_review(self):
        for attribute, value in (("guild_id", 123), ("channel_id", 123)):
            previous = getattr(self.inter, attribute)
            setattr(self.inter, attribute, value)
            await self.cog.review_button(self.inter)
            setattr(self.inter, attribute, previous)
        self.inter.message.id = 456
        await self.cog.review_button(self.inter)
        self.applicant.edit.assert_not_awaited()
        self.database.finish_promotion_request.assert_not_awaited()

    async def test_bot_hierarchy_and_mafia_hold_keep_request_open(self):
        self.bot_member.top_role = self.roles[FAMILY_RANK_ROLE_IDS["tech"]]
        await self.cog.review_button(self.inter)
        self.bot_member.top_role = self.roles[500]
        self.database.get_mafia_saved_access.return_value = 6
        await self.cog.review_button(self.inter)
        self.applicant.edit.assert_not_awaited()
        self.assertEqual(self.request["status"], "pending")

    async def test_notification_roles_follow_server(self):
        self.assertEqual(set(await self.cog.notification_role_ids(self.guild, "Seattle")),
                         {700, 600, FAMILY_RANK_ROLE_IDS["deputy leader"]})
        self.assertEqual(set(await self.cog.notification_role_ids(self.guild, None)),
                         {HIGH_FAMQ_ROLE_ID, 600, 601, FAMILY_RANK_ROLE_IDS["deputy leader"]})

    async def test_choices_are_restricted_to_owner(self):
        view = PromotionChoices(self.cog, 10, ["tech"], [{"id": 1, "name": "Seattle"}], "Seattle")
        self.assertFalse(await view.interaction_check(self.inter))
        self.inter.author = self.applicant
        self.assertTrue(await view.interaction_check(self.inter))
        self.assertEqual(view.server_name, "Seattle")

    async def test_start_form_only_offers_higher_ranks(self):
        self.inter.author = self.applicant
        await self.cog.start_request(self.inter)
        view = self.inter.edit_original_response.call_args.kwargs["view"]
        choices = [option.value for option in view.rank_select.options]
        self.assertNotIn("young", choices)
        self.assertNotIn("old", choices)
        self.assertIn("tech", choices)
        self.assertEqual(view.server_name, "Seattle")

    async def test_submission_pings_roles_and_persists_message(self):
        channel = Mock(spec=disnake.TextChannel)
        channel.guild = self.guild
        channel.send = AsyncMock(return_value=SimpleNamespace(id=123))
        self.bot.get_channel = Mock(return_value=channel)
        self.inter.author = self.applicant
        await self.cog.submit_request(self.inter, "tech", "Seattle", "Work", "Proof")
        sent = channel.send.call_args.kwargs
        self.assertEqual(set(sent["allowed_mentions"].to_dict()["roles"]),
                         {700, 600, FAMILY_RANK_ROLE_IDS["deputy leader"]})
        self.assertEqual(sent["allowed_mentions"].to_dict()["parse"], [])
        self.database.set_promotion_log_message.assert_awaited_once_with(1, 123)

    async def test_submission_rejects_stale_rank_and_server(self):
        self.inter.author = self.applicant
        for rank, server in (("old", "Seattle"), ("young", "Seattle"), ("tech", "Deleted")):
            await self.cog.submit_request(self.inter, rank, server, "Work", "")
        self.database.create_promotion_request.assert_not_awaited()

    async def test_failed_publication_cancels_unpublished_request(self):
        channel = Mock(spec=disnake.TextChannel)
        channel.guild = self.guild
        channel.send = AsyncMock(side_effect=disnake.DiscordException("denied"))
        self.bot.get_channel = Mock(return_value=channel)
        self.inter.author = self.applicant
        with self.assertLogs(level="ERROR"):
            await self.cog.submit_request(self.inter, "tech", "Seattle", "Work", "")
        self.database.cancel_unpublished_promotion.assert_awaited_once_with(1)

    def test_full_achievement_text_fits_discord_fields(self):
        self.request["achievements"] = "x" * 1500
        fields = promotion_embed(self.request).fields
        self.assertTrue(all(len(field.value) <= 1024 for field in fields))
        self.assertEqual(sum(len(field.value) for field in fields if field.name in {"Что сделано", "Продолжение"}), 1500)


if __name__ == "__main__":
    unittest.main()
