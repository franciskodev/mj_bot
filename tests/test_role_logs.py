from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import disnake

from cogs.role_logs import RoleLogsCog
from config import BROADCAST_GUILD_ID, GUILD_ID, ROLE_LOG_CHANNEL_ID


class RoleLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(
            id=GUILD_ID, _state=Mock(), get_role=Mock(return_value=None),
            get_channel=Mock(return_value=None),
            get_member=Mock(return_value=None), get_channel_or_thread=Mock(return_value=None),
        )
        self.cog = RoleLogsCog(SimpleNamespace(user=SimpleNamespace(id=900)))
        self.log = AsyncMock()
        patcher = patch("cogs.role_logs.send_admin_log", self.log)
        patcher.start()
        self.addCleanup(patcher.stop)

    def entry(self, action, changes, **data):
        return disnake.AuditLogEntry(
            guild=self.guild,
            data={
                "id": str(disnake.utils.time_snowflake(disnake.utils.utcnow())),
                "action_type": action.value, "user_id": "900", "target_id": "123",
                "changes": changes, "reason": "Проверка", **data,
            },
            application_commands={}, automod_rules={}, guild_scheduled_events={},
            integrations={}, threads={}, users={}, webhooks={},
        )

    async def test_bot_additions_and_removals_with_uncached_members_and_roles(self):
        await self.cog.log_role_action(self.entry(disnake.AuditLogAction.member_role_update, [
            {"key": "$add", "new_value": [{"id": "10", "name": "Новая"}]},
            {"key": "$remove", "new_value": [{"id": "11", "name": "Старая"}]},
        ]))
        self.log.assert_awaited_once()
        output = self.log.call_args.kwargs
        self.assertEqual(output["channel_id"], ROLE_LOG_CHANNEL_ID)
        for value in ("<@900>", "<@123>", "Выданы роли", "Сняты роли",
                      "<@&10>", "Новая", "<@&11>", "Старая", "Проверка"):
            self.assertIn(value, output["description"])

    async def test_role_lifecycle_and_permission_changes(self):
        for action in (disnake.AuditLogAction.role_create, disnake.AuditLogAction.role_update,
                       disnake.AuditLogAction.role_delete):
            with self.subTest(action=action):
                self.log.reset_mock()
                change = {"key": "name"}
                if action != disnake.AuditLogAction.role_create:
                    change["old_value"] = "Старая"
                if action != disnake.AuditLogAction.role_delete:
                    change["new_value"] = "Новая"
                await self.cog.log_role_action(self.entry(action, [change, {
                    "key": "permissions", "old_value": "0", "new_value": "8",
                }, {"key": "position", "old_value": 1, "new_value": 2}]))
                output = self.log.call_args.kwargs["description"]
                self.assertIn("<@&123>", output)
                self.assertIn("administrator", output)
                self.assertIn("Позиция:** 1 → 2", output)
                self.assertIn("Старая" if action == disnake.AuditLogAction.role_delete else "Новая", output)

    async def test_channel_overwrites_only_log_roles(self):
        for action in (disnake.AuditLogAction.overwrite_create,
                       disnake.AuditLogAction.overwrite_update,
                       disnake.AuditLogAction.overwrite_delete):
            with self.subTest(action=action):
                self.log.reset_mock()
                changes = [{"key": "allow", "old_value": "0", "new_value": "1024"}]
                await self.cog.log_role_action(self.entry(
                    action, changes, options={"id": "10", "type": "1"},
                ))
                self.log.assert_not_awaited()
                await self.cog.log_role_action(self.entry(
                    action, changes, options={"id": "10", "type": "0", "role_name": "Роль"},
                ))
                self.log.assert_awaited_once()
                output = self.log.call_args.kwargs["description"]
                self.assertIn("<@&10>", output)
                self.assertIn("<#123>", output)
                self.assertIn("view_channel", output)

    async def test_unrelated_actions_and_other_guilds_are_ignored(self):
        await self.cog.log_role_action(self.entry(disnake.AuditLogAction.member_update, []))
        self.guild.id = BROADCAST_GUILD_ID
        await self.cog.log_role_action(self.entry(disnake.AuditLogAction.role_create, []))
        self.log.assert_not_awaited()

    async def test_large_role_changes_preserve_all_roles_within_embed_limits(self):
        roles = [{"id": str(index), "name": "Роль" * 25} for index in range(1, 251)]
        await self.cog.log_role_action(self.entry(disnake.AuditLogAction.member_role_update, [
            {"key": "$add", "new_value": roles},
        ], user_id=None, reason=None))
        self.assertGreater(self.log.await_count, 1)
        pages = [call.kwargs["description"] for call in self.log.call_args_list]
        self.assertTrue(all(len(page) <= 4096 for page in pages))
        self.assertIn("Исполнитель: Неизвестно", pages[0])
        combined = "\n".join(pages)
        for role in roles:
            self.assertIn(f"<@&{role['id']}>", combined)


if __name__ == "__main__":
    unittest.main()
