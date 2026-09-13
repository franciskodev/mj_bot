import logging

import disnake

from config import ADMIN_LOG_CHANNEL_ID, APPLICATION_LOG_CHANNEL_ID, GUILD_ID, SECURITY_LOG_CHANNEL_ID


async def send_security_log(
    bot: disnake.Client, *, title: str, description: str,
    color: disnake.Color = disnake.Color.red(),
) -> None:
    for channel_id in dict.fromkeys((ADMIN_LOG_CHANNEL_ID, SECURITY_LOG_CHANNEL_ID)):
        await send_admin_log(
            bot, channel_id=channel_id, title=title, description=description, color=color,
        )


async def application_log_channel(bot: disnake.Client) -> disnake.TextChannel | None:
    """Keep the shared archive, including old attachments, administrator-only."""
    channel = bot.get_channel(APPLICATION_LOG_CHANNEL_ID)
    try:
        if channel is None:
            channel = await bot.fetch_channel(APPLICATION_LOG_CHANNEL_ID)
        if not isinstance(channel, disnake.TextChannel) or channel.guild.id != GUILD_ID:
            return None
        guild = channel.guild
        if guild.me is None:
            return None
        overwrites = {
            guild.default_role: disnake.PermissionOverwrite(view_channel=False),
            guild.me: disnake.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            ),
        }
        if channel.overwrites != overwrites:
            await channel.edit(overwrites=overwrites, reason="Приватный архив заявок INTERPOLOV")
        return channel
    except disnake.DiscordException:
        logging.exception("Could not secure the application archive")
        return None


async def send_admin_log(
    bot: disnake.Client,
    *,
    title: str,
    description: str,
    color: disnake.Color = disnake.Color.blurple(),
    channel_id: int = ADMIN_LOG_CHANNEL_ID,
) -> None:
    if channel_id == APPLICATION_LOG_CHANNEL_ID:
        channel = await application_log_channel(bot)
        if channel is None:
            return
    else:
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
    embed.set_footer(text="INTERPOLOV • Журнал действий")
    try:
        await channel.send(embed=embed)
    except disnake.DiscordException:
        logging.exception("Could not send log to channel %s", channel_id)
