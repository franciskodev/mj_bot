import logging

import disnake

from config import ADMIN_LOG_CHANNEL_ID


async def send_admin_log(
    bot: disnake.Client,
    *,
    title: str,
    description: str,
    color: disnake.Color = disnake.Color.blurple(),
    channel_id: int = ADMIN_LOG_CHANNEL_ID,
) -> None:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except disnake.DiscordException:
            logging.exception("Could not fetch log channel %s", channel_id)
            return
    if not isinstance(channel, (disnake.TextChannel, disnake.Thread)):
        logging.error("Log channel %s is not a text channel", channel_id)
        return
    embed = disnake.Embed(
        title=title, description=description, color=color, timestamp=disnake.utils.utcnow()
    )
    embed.set_footer(text="INTERPOLOV & INTOLERANCE • Журнал действий")
    try:
        await channel.send(embed=embed)
    except disnake.DiscordException:
        logging.exception("Could not send log to channel %s", channel_id)
