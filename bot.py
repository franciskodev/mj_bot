import disnake
from disnake.ext import commands

from access_control import (
    ACCESS_LEVEL_NAMES,
    command_access_level,
    interaction_command_name,
    member_access_level,
)
from cogs.activity import ActivityCog
from cogs.applications import ApplicationsCog
from cogs.broadcast import BroadcastCog
from cogs.core import CoreCog
from cogs.direct_messages import DirectMessagesCog
from cogs.general import GeneralCog
from cogs.leave import LeaveCog
from cogs.meetings import MeetingsCog
from cogs.management import ManagementCog
from cogs.server_tags import ServerTagsCog
from cogs.systems import SystemsCog
from cogs.users import UsersCog
from cogs.warnings import WarningsCog
from database import Database


class IntoleranceBot(commands.InteractionBot):
    def __init__(self) -> None:
        intents = disnake.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(intents=intents)

        self.database = Database()
        self.add_app_command_check(
            self.check_command_access,
            slash_commands=True,
        )
        self.add_cog(CoreCog(self))
        self.add_cog(DirectMessagesCog(self))
        self.add_cog(GeneralCog(self))
        self.add_cog(BroadcastCog(self))
        self.add_cog(UsersCog(self))
        self.add_cog(ActivityCog(self))
        self.add_cog(LeaveCog(self))
        self.add_cog(MeetingsCog(self))
        self.add_cog(ManagementCog(self))
        self.add_cog(ServerTagsCog(self))
        self.add_cog(WarningsCog(self))
        self.add_cog(ApplicationsCog(self))
        self.add_cog(SystemsCog(self))

    async def check_command_access(
        self, inter: disnake.ApplicationCommandInteraction
    ) -> bool:
        if inter.guild_id is None or not isinstance(inter.author, disnake.Member):
            return False
        command_name = interaction_command_name(inter)
        required = await command_access_level(self, inter.guild_id, command_name)
        actual = await member_access_level(self, inter.author)
        if actual >= required:
            return True
        if not inter.response.is_done():
            await inter.response.send_message(
                f"Недостаточно прав. Для `/{command_name}` нужен уровень "
                f"**{required} — {ACCESS_LEVEL_NAMES[required]}**. "
                f"Ваш уровень: **{actual} — {ACCESS_LEVEL_NAMES[actual]}**.",
                ephemeral=True,
            )
        return False
