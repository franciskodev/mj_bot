from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

import disnake

from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import InterpolovBot


DM_DISABLED_REASON = "не отправлено: система уведомлений в ЛС отключена"


@dataclass(frozen=True)
class DMDeliveryResult:
    delivered: bool
    reason: str
    disabled: bool = False


async def send_dm_notification(
    bot: "InterpolovBot",
    recipient: disnake.abc.User,
    *,
    context: str,
    content: str | None = None,
    embed: disnake.Embed | None = None,
) -> DMDeliveryResult:
    if not await bot.database.get_dm_notifications_enabled():
        await send_admin_log(
            bot,
            title="🔕 Уведомление в ЛС не отправлено",
            description=(
                f"Пользователь: {recipient.mention} (`{recipient.id}`)\n"
                f"Событие: **{context}**\nПричина: **{DM_DISABLED_REASON}**"
            ),
            color=disnake.Color.orange(),
        )
        return DMDeliveryResult(False, DM_DISABLED_REASON, disabled=True)

    try:
        await recipient.send(content=content, embed=embed)
    except disnake.DiscordException as error:
        logging.warning("Could not send %s DM to %s: %s", context, recipient.id, error)
        return DMDeliveryResult(False, "не удалось доставить через Discord")
    return DMDeliveryResult(True, "доставлено")
