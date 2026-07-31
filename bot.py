import disnake
from disnake.ext import commands

from cogs.activity import ActivityCog
from cogs.applications import ApplicationsCog
from cogs.broadcast import BroadcastCog
from cogs.core import CoreCog
from cogs.direct_messages import DirectMessagesCog
from cogs.general import GeneralCog
from cogs.leave import LeaveCog
from cogs.meetings import MeetingsCog
from cogs.users import UsersCog
from database import Database


class InterpolBot(commands.InteractionBot):
    def __init__(self) -> None:
        intents = disnake.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(intents=intents)

        self.database = Database()
        self.add_cog(CoreCog(self))
        self.add_cog(DirectMessagesCog(self))
        self.add_cog(GeneralCog(self))
        self.add_cog(BroadcastCog(self))
        self.add_cog(UsersCog(self))
        self.add_cog(ActivityCog(self))
        self.add_cog(LeaveCog(self))
        self.add_cog(MeetingsCog(self))
        self.add_cog(ApplicationsCog(self))
