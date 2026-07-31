import logging

import disnake

from config import ADMIN_LOG_CHANNEL_ID


async def send_admin_log(
    bot: disnake.Client,
    *,
    title: str,
    description: str,
    color: disnake.Color = disnake.Color.blurple(),
) -> None:
    channel = bot.get_channel(ADMIN_LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ADMIN_LOG_CHANNEL_ID)
        except disnake.DiscordException:
            logging.exception("Could not fetch admin log channel")
            return
    if not isinstance(channel, (disnake.TextChannel, disnake.Thread)):
        logging.error("Admin log channel %s is not a text channel", ADMIN_LOG_CHANNEL_ID)
        return
    embed = disnake.Embed(
        title=title, description=description, color=color, timestamp=disnake.utils.utcnow()
    )
    embed.set_footer(text="INTERPOL • Журнал действий")
    try:
        await channel.send(embed=embed)
    except disnake.DiscordException:
        logging.exception("Could not send admin log")
