import logging
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from config import FORWARD_CHANNEL_ID

if TYPE_CHECKING:
    from bot import InterpolBot


class DirectMessagesCog(commands.Cog):
    def __init__(self, bot: "InterpolBot") -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message) -> None:
        if message.author.bot or not isinstance(message.channel, disnake.DMChannel):
            return

        await self.forward_dm(message)

    async def get_forward_channel(self) -> disnake.abc.Messageable | None:
        channel = self.bot.get_channel(FORWARD_CHANNEL_ID)

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(FORWARD_CHANNEL_ID)
            except disnake.DiscordException:
                logging.exception("Could not fetch DM forward channel")
                return None

        if isinstance(channel, (disnake.TextChannel, disnake.Thread)):
            return channel

        logging.error("Forward channel %s is not a text channel", FORWARD_CHANNEL_ID)
        return None

    async def forward_dm(self, message: disnake.Message) -> None:
        channel = await self.get_forward_channel()
        if channel is None:
            return

        embed = disnake.Embed(
            title="New DM",
            description=message.content or None,
            color=disnake.Color.blurple(),
        )
        embed.set_author(
            name=f"{message.author} ({message.author.id})",
            icon_url=message.author.display_avatar.url,
        )

        if message.attachments:
            files_text = "\n".join(attachment.url for attachment in message.attachments)
            embed.add_field(name="Attachments", value=files_text[:1024], inline=False)

        await channel.send(embed=embed)
