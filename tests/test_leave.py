import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import disnake

from cogs.leave import LeaveCog
from config import BROADCAST_GUILD_ID, GUILD_ID, HIGH_FAMQ_ROLE_ID


class LeaveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = SimpleNamespace(
            get_application_staff_roles=AsyncMock(return_value=[
                {"server_name": "Seattle", "access_level": 2, "role_id": 10},
                {"server_name": "Seattle", "access_level": 4, "role_id": 11},
                {"server_name": "Orlando", "access_level": 4, "role_id": 21},
            ]),
            get_user=AsyncMock(return_value={"server_name": "Seattle", "game_nickname": "Test"}),
            create_leave_request=AsyncMock(return_value={
                "id": 1, "ends_at": disnake.utils.utcnow() + timedelta(days=2),
            }),
            set_leave_log_message=AsyncMock(),
            review_leave=AsyncMock(return_value={"discord_id": 100}),
        )
        self.channel = Mock(spec=disnake.TextChannel)
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=200))
        self.bot = SimpleNamespace(database=self.database, get_channel=Mock(return_value=self.channel))
        with patch.object(LeaveCog, "sync_leave_roles"):
            self.cog = LeaveCog(self.bot)
        self.guild = SimpleNamespace(
            id=GUILD_ID, name="Test", get_role=lambda role_id: role_id,
            get_member=Mock(return_value=None), fetch_member=AsyncMock(return_value=None),
        )
        self.author = Mock(spec=disnake.Member)
        self.author.id = 500
        self.author.mention = "<@500>"
        self.author.display_avatar = SimpleNamespace(url="https://example.com/avatar.png")
        self.author.guild_permissions = disnake.Permissions.none()
        self.inter = SimpleNamespace(
            author=self.author, guild=self.guild, guild_id=GUILD_ID,
            component=SimpleNamespace(custom_id="leave:approved:1"),
            message=SimpleNamespace(embeds=[disnake.Embed(title="Leave")]),
            response=SimpleNamespace(
                send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock(),
            ),
            edit_original_response=AsyncMock(),
        )

    async def test_notification_roles_follow_profile_servers(self):
        for server, expected in (
            ("Seattle", [11]),
            (" orLANDO ", [21]),
            ("Seattle, Orlando, Seattle", [11, 21]),
            (None, [HIGH_FAMQ_ROLE_ID]),
            ("", [HIGH_FAMQ_ROLE_ID]),
            ("-", [HIGH_FAMQ_ROLE_ID]),
            ("—", [HIGH_FAMQ_ROLE_ID]),
            ("Denver", [HIGH_FAMQ_ROLE_ID]),
        ):
            with self.subTest(server=server):
                self.assertEqual(
                    await self.cog.leave_notification_role_ids(self.guild, server), expected
                )
        self.guild.get_role = lambda role_id: None
        self.assertEqual(
            await self.cog.leave_notification_role_ids(self.guild, "Seattle"),
            [HIGH_FAMQ_ROLE_ID],
        )

    async def test_submission_sends_explicit_role_ping(self):
        self.cog.get_leave_request_error = AsyncMock(return_value=None)
        for server, role_id in (("Seattle", 11), (None, HIGH_FAMQ_ROLE_ID)):
            with self.subTest(server=server):
                self.database.get_user.return_value["server_name"] = server
                await self.cog.submit_leave_request(self.inter, "Reason", 2)
                sent = self.channel.send.call_args.kwargs
                self.assertEqual(sent["content"], f"<@&{role_id}>")
                mentions = sent["allowed_mentions"].to_dict()
                self.assertEqual(mentions["roles"], [role_id])
                self.assertEqual(mentions["parse"], [])
                self.assertEqual(len(sent["components"]), 2)
                self.database.set_leave_log_message.assert_awaited_with(1, 200)

    async def test_review_allows_every_level_four_and_above(self):
        self.cog.set_leave_role = AsyncMock()
        for status in ("approved", "declined"):
            for level in range(7):
                with self.subTest(status=status, level=level):
                    self.database.review_leave.reset_mock()
                    self.cog.set_leave_role.reset_mock()
                    self.inter.component.custom_id = f"leave:{status}:1"
                    with patch("cogs.leave.member_access_level", AsyncMock(return_value=level)), patch(
                        "cogs.leave.send_admin_log", AsyncMock()
                    ):
                        await self.cog.review_button(self.inter)
                    if level < 4:
                        self.database.review_leave.assert_not_awaited()
                    else:
                        self.database.review_leave.assert_awaited_once_with(1, status, 500)
                    if level >= 4 and status == "approved":
                        self.cog.set_leave_role.assert_awaited_once_with(100, enabled=True)
                    else:
                        self.cog.set_leave_role.assert_not_awaited()

    async def test_review_rejects_other_guilds_and_invalid_actions(self):
        with patch("cogs.leave.member_access_level", AsyncMock(return_value=6)):
            for guild_id in (None, BROADCAST_GUILD_ID):
                self.inter.guild_id = guild_id
                await self.cog.review_button(self.inter)
            self.inter.guild_id = GUILD_ID
            for custom_id in (None, "leave:other:1", "leave:approved:invalid"):
                self.inter.component.custom_id = custom_id
                await self.cog.review_button(self.inter)
        self.database.review_leave.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
