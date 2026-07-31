import logging
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from config import ADMIN_LOG_CHANNEL_ID, GUILD_ID
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import IntoleranceBot


class LeaveCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot

    @commands.slash_command(name="leave", description="Управление отпуском", guild_ids=[GUILD_ID])
    async def leave(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @leave.sub_command(name="request", description="Подать запрос на отпуск")
    async def leave_request(
        self,
        inter: disnake.ApplicationCommandInteraction,
        reason: str = commands.Param(description="Причина отпуска", max_length=500),
        days: int = commands.Param(description="Продолжительность в днях", ge=1, le=30),
    ) -> None:
        profile = await self.bot.database.get_user(inter.author.id)
        if profile is None:
            await inter.response.send_message(
                "Ваш профиль отсутствует в базе. Обратитесь к администратору.", ephemeral=True
            )
            return
        if not profile["is_active"]:
            await inter.response.send_message(
                f"Ваш профиль неактивен: **{profile['inactive_reason'] or 'причина не указана'}**",
                ephemeral=True,
            )
            return
        if await self.bot.database.has_pending_leave(inter.author.id):
            await inter.response.send_message("У вас уже есть запрос на рассмотрении.", ephemeral=True)
            return

        request = await self.bot.database.create_leave_request(inter.author.id, reason, days)
        channel = self.bot.get_channel(ADMIN_LOG_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(ADMIN_LOG_CHANNEL_ID)
        if not isinstance(channel, (disnake.TextChannel, disnake.Thread)):
            await inter.response.send_message("Канал заявок недоступен.", ephemeral=True)
            return

        embed = disnake.Embed(
            title="🏖️ Запрос на отпуск",
            description=f"### {inter.author.mention}\nНовая заявка ожидает решения администрации.",
            color=disnake.Color.gold(),
            timestamp=disnake.utils.utcnow(),
        )
        embed.set_thumbnail(url=inter.author.display_avatar.url)
        embed.add_field(name="🎮 Игровой ник", value=profile["game_nickname"] or "—", inline=True)
        embed.add_field(name="🌐 Сервер", value=profile["server_name"] or "—", inline=True)
        embed.add_field(name="📆 Срок", value=f"**{days}** дн.\nДо <t:{int(request['ends_at'].timestamp())}:D>", inline=True)
        embed.add_field(name="📝 Причина", value=reason, inline=False)
        embed.add_field(name="🆔 Discord ID", value=f"`{inter.author.id}`", inline=True)
        embed.set_footer(text=f"INTOLERANCE • Заявка #{request['id']}")
        components = [
            disnake.ui.Button(label="Одобрить", emoji="✅", style=disnake.ButtonStyle.success, custom_id=f"leave:approved:{request['id']}"),
            disnake.ui.Button(label="Отклонить", emoji="✖️", style=disnake.ButtonStyle.danger, custom_id=f"leave:declined:{request['id']}"),
        ]
        message = await channel.send(embed=embed, components=components)
        await self.bot.database.set_leave_log_message(request["id"], message.id)
        await inter.response.send_message(
            f"✅ Заявка **#{request['id']}** отправлена администрации.", ephemeral=True
        )

    @commands.Cog.listener("on_button_click")
    async def review_button(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id
        if not custom_id.startswith("leave:"):
            return
        if not isinstance(inter.author, disnake.Member) or not inter.author.guild_permissions.administrator:
            await inter.response.send_message("Только администратор может принять решение.", ephemeral=True)
            return
        try:
            _, status, raw_id = custom_id.split(":", 2)
            request_id = int(raw_id)
        except (ValueError, AttributeError):
            return
        request = await self.bot.database.review_leave(request_id, status, inter.author.id)
        if request is None:
            await inter.response.send_message("Эта заявка уже обработана.", ephemeral=True)
            return

        approved = status == "approved"
        embed = inter.message.embeds[0]
        embed.title = "✅ Отпуск одобрен" if approved else "❌ Отпуск отклонён"
        embed.color = disnake.Color.green() if approved else disnake.Color.red()
        embed.add_field(name="👮 Решение", value=f"{inter.author.mention}\n<t:{int(disnake.utils.utcnow().timestamp())}:f>", inline=False)
        await inter.response.edit_message(embed=embed, components=[])
        await send_admin_log(
            self.bot,
            title="✅ Отпуск одобрен" if approved else "❌ Отпуск отклонён",
            description=f"Администратор: {inter.author.mention}\nПользователь: <@{request['discord_id']}>\nЗаявка: `#{request_id}`",
            color=disnake.Color.green() if approved else disnake.Color.red(),
        )
