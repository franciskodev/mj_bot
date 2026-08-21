import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from config import FORWARD_CHANNEL_ID, GUILD_ID

if TYPE_CHECKING:
    from bot import IntoleranceBot


DM_TIMEOUT_DURATION = timedelta(minutes=10)
DM_TIMEOUT_REASON = "Запрещённое слово в личном сообщении боту"
DM_TIMEOUT_SUCCESS_REPLY = "иди нахуй, ты теперь в муте лох"
DM_TIMEOUT_FAILURE_REPLY = "иди нахуй, жаль я тебе мут не могу дать, так бы трахнул"
DM_TRIGGER_WORDS_PATH = Path(__file__).resolve().parent.parent / "dm_trigger_words.txt"


def load_dm_trigger_words() -> frozenset[str]:
    return frozenset(
        word.strip().casefold()
        for word in DM_TRIGGER_WORDS_PATH.read_text(encoding="utf-8").split(",")
        if word.strip()
    )


DM_TRIGGER_WORDS = load_dm_trigger_words()
DM_TRIGGER_PATTERN = re.compile(
    rf"(?<!\w)(?:{'|'.join(re.escape(word) for word in sorted(DM_TRIGGER_WORDS, key=len, reverse=True))})(?!\w)"
)


def contains_dm_trigger_word(content: str) -> bool:
    return DM_TRIGGER_PATTERN.search(content.casefold()) is not None


class DirectMessagesCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message) -> None:
        if message.author.bot or not isinstance(message.channel, disnake.DMChannel):
            return

        if contains_dm_trigger_word(message.content):
            timeout_applied = await self.timeout_main_guild_member(message.author.id)
            reply_text = (
                DM_TIMEOUT_SUCCESS_REPLY
                if timeout_applied
                else DM_TIMEOUT_FAILURE_REPLY
            )
            try:
                await message.channel.send(reply_text)
            except disnake.DiscordException:
                logging.exception("Could not send automatic DM reply to %s", message.author.id)

        await self.forward_dm(message)

    async def timeout_main_guild_member(self, user_id: int) -> bool:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            logging.error("Could not apply DM timeout because guild %s is unavailable", GUILD_ID)
            return False

        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except disnake.DiscordException:
                logging.info("DM author %s is not available in guild %s", user_id, GUILD_ID)
                return False

        try:
            await member.timeout(
                duration=DM_TIMEOUT_DURATION,
                reason=DM_TIMEOUT_REASON,
            )
        except disnake.DiscordException:
            logging.exception("Could not apply DM timeout to member %s", user_id)
            return False

        return True

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
