import asyncio
import logging
from datetime import datetime

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
from cogs.chat_responses import ChatResponsesCog
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
from config import BROADCAST_GUILD_ID, GUILD_ID
from database import Database


class IntoleranceBot(commands.InteractionBot):
    def __init__(self) -> None:
        intents = disnake.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(intents=intents)

        self.database = Database()
        self.voice_shutdown_at: datetime | None = None
        self.fatal_error: BaseException | None = None
        self._fatal_shutdown_task: asyncio.Task[None] | None = None
        self._close_lock = asyncio.Lock()
        self.add_app_command_check(
            self.check_command_access,
            slash_commands=True,
        )
        self.add_cog(CoreCog(self))
        self.add_cog(ChatResponsesCog(self))
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

    def request_fatal_shutdown(self, error: BaseException) -> None:
        if self.fatal_error is None:
            self.fatal_error = error
            logging.critical(
                "Stopping the bot because an exclusive database lock was lost",
                exc_info=error,
            )
        if self._fatal_shutdown_task is None or self._fatal_shutdown_task.done():
            self._fatal_shutdown_task = asyncio.create_task(
                self.close(),
                name="fatal-bot-shutdown",
            )

    async def close(self) -> None:
        async with self._close_lock:
            if self.is_closed():
                return
            activity_cog = self.get_cog("ActivityCog")
            if isinstance(activity_cog, ActivityCog):
                shutdown_at = await activity_cog.shutdown_voice_activity()
                if (
                    self.voice_shutdown_at is None
                    or shutdown_at < self.voice_shutdown_at
                ):
                    self.voice_shutdown_at = shutdown_at
            await super().close()

    async def check_command_access(
        self, inter: disnake.ApplicationCommandInteraction
    ) -> bool:
        if inter.guild_id is None or not isinstance(inter.author, disnake.Member):
            return False
        command_name = interaction_command_name(inter)
        if inter.guild_id != GUILD_ID and not (
            inter.guild_id == BROADCAST_GUILD_ID and command_name == "broadcast"
        ):
            return False
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
