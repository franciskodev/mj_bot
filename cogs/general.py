from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from config import GUILD_ID

if TYPE_CHECKING:
    from bot import InterpolBot


class GeneralCog(commands.Cog):
    def __init__(self, bot: "InterpolBot") -> None:
        self.bot = bot

    @commands.slash_command(name="ping", description="Check bot latency", guild_ids=[GUILD_ID])
    async def slash_ping(self, inter: disnake.ApplicationCommandInteraction) -> None:
        await inter.response.send_message(f"Pong! `{round(self.bot.latency * 1000)} ms`")
