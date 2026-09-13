import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.family_ranks import family_rank_from_roles, recruitment_positions_from_roles
from config import (
    BROADCAST_GUILD_ID,
    FAMILY_RANK_ROLE_IDS,
    GUILD_ID,
    RECRUITMENT_POSITION_ROLE_IDS,
    TARGET_ROLE_IDS,
)
from database import Database


class FamilyRankTests(unittest.TestCase):
    def test_default_and_each_rank(self):
        self.assertEqual(family_rank_from_roles(set()), "young")
        for rank, role_id in FAMILY_RANK_ROLE_IDS.items():
            with self.subTest(rank=rank):
                self.assertEqual(family_rank_from_roles({role_id}), rank)
                self.assertIn(role_id, TARGET_ROLE_IDS)

    def test_highest_rank_wins_and_positions_do_not_change_rank(self):
        held = set(RECRUITMENT_POSITION_ROLE_IDS.values()) | {100}
        self.assertEqual(family_rank_from_roles(held), "young")
        for rank, role_id in FAMILY_RANK_ROLE_IDS.items():
            held.add(role_id)
            self.assertEqual(family_rank_from_roles(held), rank)
            self.assertEqual(
                recruitment_positions_from_roles(held, {100}),
                ["Рекрутёр", "Deputy Chief Recruit", "Chief Recruit"],
            )

    def test_removing_position_does_not_change_rank(self):
        held = {FAMILY_RANK_ROLE_IDS["old"], RECRUITMENT_POSITION_ROLE_IDS["Chief Recruit"]}
        self.assertEqual(recruitment_positions_from_roles(held, {100}), ["Chief Recruit"])
        held.remove(RECRUITMENT_POSITION_ROLE_IDS["Chief Recruit"])
        self.assertEqual(recruitment_positions_from_roles(held, {100}), [])
        self.assertEqual(family_rank_from_roles(held), "old")


class FamilyRankPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database()
        self.db.pool = SimpleNamespace(fetch=AsyncMock(), execute=AsyncMock())
        self.member = SimpleNamespace(
            id=100, bot=False, guild=SimpleNamespace(id=GUILD_ID),
            roles=[SimpleNamespace(id=FAMILY_RANK_ROLE_IDS["young"])],
        )

    async def test_mafia_held_roles_preserve_rank_and_position(self):
        self.db.pool.fetch.side_effect = [
            [{"role_ids": [FAMILY_RANK_ROLE_IDS["owner"], RECRUITMENT_POSITION_ROLE_IDS["Chief Recruit"]]}],
            [],
        ]
        await self.db.sync_user_family_roles(self.member)
        self.assertEqual(self.db.pool.execute.call_args.args[1:], (100, "owner", ["Chief Recruit"]))

    async def test_recruiter_role_is_saved_separately(self):
        self.member.roles.append(SimpleNamespace(id=200))
        self.db.pool.fetch.side_effect = [[], [{"role_id": 200}]]
        await self.db.sync_user_family_roles(self.member)
        self.assertEqual(self.db.pool.execute.call_args.args[1:], (100, "young", ["Рекрутёр"]))

    async def test_other_guilds_and_bots_are_ignored(self):
        self.member.guild.id = BROADCAST_GUILD_ID
        await self.db.sync_user_family_roles(self.member)
        self.member.guild.id = GUILD_ID
        self.member.bot = True
        await self.db.sync_user_family_roles(self.member)
        self.db.pool.fetch.assert_not_awaited()
        self.db.pool.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
