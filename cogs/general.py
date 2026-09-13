from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from config import GUILD_IDS

if TYPE_CHECKING:
    from bot import InterpolovBot


class GeneralCog(commands.Cog):
    def __init__(self, bot: "InterpolovBot") -> None:
        self.bot = bot

    @commands.slash_command(name="ping", description="Check bot latency", guild_ids=GUILD_IDS)
    async def slash_ping(self, inter: disnake.ApplicationCommandInteraction) -> None:
        await inter.response.send_message(f"Pong! `{round(self.bot.latency * 1000)} ms`")
