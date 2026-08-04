import logging
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands, tasks

from config import GUILD_ID, GUILD_IDS, LEAVE_ROLE_ID, MANAGEMENT_LOG_CHANNEL_ID, TARGET_ROLE_IDS
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import IntoleranceBot


class LeaveRequestModal(disnake.ui.Modal):
    def __init__(self, cog: "LeaveCog") -> None:
        self.cog = cog
        super().__init__(
            title="Заявка на отпуск",
            custom_id="leave:request:modal",
            components=[
                disnake.ui.TextInput(
                    label="Причина отпуска",
                    custom_id="reason",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=500,
                ),
                disnake.ui.TextInput(
                    label="Количество дней (от 1 до 30)",
                    custom_id="days",
                    max_length=2,
                ),
                disnake.ui.TextInput(
                    label="Доказательства (при наличии)",
                    custom_id="evidence",
                    style=disnake.TextInputStyle.paragraph,
                    required=False,
                    max_length=1000,
                ),
            ],
            timeout=600,
        )

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        reason = inter.text_values["reason"].strip()
        if not reason:
            await inter.response.send_message("Укажите причину отпуска.", ephemeral=True)
            return
        try:
            days = int(inter.text_values["days"].strip())
        except ValueError:
            await inter.response.send_message(
                "Количество дней должно быть указано числом.", ephemeral=True
            )
            return
        if not 1 <= days <= 30:
            await inter.response.send_message(
                "Укажите продолжительность отпуска от 1 до 30 дней.", ephemeral=True
            )
            return

        await self.cog.submit_leave_request(
            inter,
            reason,
            days,
            inter.text_values["evidence"].strip(),
        )


class LeaveCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot
        self.sync_leave_roles.start()

    def cog_unload(self) -> None:
        self.sync_leave_roles.cancel()

    async def set_leave_role(self, discord_id: int, *, enabled: bool) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        role = guild.get_role(LEAVE_ROLE_ID)
        if role is None:
            logging.warning("Leave role %s was not found in guild %s", LEAVE_ROLE_ID, GUILD_ID)
            return
        member = guild.get_member(discord_id)
        try:
            if member is None:
                member = await guild.fetch_member(discord_id)
            if enabled and role not in member.roles:
                await member.add_roles(role, reason="Одобрен отпуск")
            elif not enabled and role in member.roles:
                await member.remove_roles(role, reason="Отпуск завершён")
        except disnake.NotFound:
            return
        except disnake.HTTPException:
            logging.exception("Failed to update leave role for member %s", discord_id)

    @tasks.loop(minutes=5)
    async def sync_leave_roles(self) -> None:
        try:
            guild = self.bot.get_guild(GUILD_ID)
            if guild is None:
                return
            role = guild.get_role(LEAVE_ROLE_ID)
            if role is None:
                logging.warning("Leave role %s was not found in guild %s", LEAVE_ROLE_ID, GUILD_ID)
                return
            active_user_ids = await self.bot.database.get_active_leave_user_ids()
            for discord_id in active_user_ids:
                await self.set_leave_role(discord_id, enabled=True)
            for member in role.members:
                if member.id not in active_user_ids:
                    await self.set_leave_role(member.id, enabled=False)
        except Exception:
            logging.exception("Leave role synchronization failed")

    @sync_leave_roles.before_loop
    async def before_sync_leave_roles(self) -> None:
        await self.bot.wait_until_ready()

    @commands.slash_command(name="leave", description="Управление отпуском", guild_ids=GUILD_IDS)
    async def leave(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @leave.sub_command(name="request", description="Подать запрос на отпуск")
    async def leave_request(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ) -> None:
        error = await self.get_leave_request_error(inter)
        if error is not None:
            await inter.response.send_message(error, ephemeral=True)
            return
        await inter.response.send_modal(modal=LeaveRequestModal(self))

    async def get_leave_request_error(
        self,
        inter: disnake.ApplicationCommandInteraction | disnake.ModalInteraction,
    ) -> str | None:
        family_role_ids = set(TARGET_ROLE_IDS)
        config = await self.bot.database.get_application_config(inter.guild_id)
        if config is not None:
            family_role_ids.update(
                {config["interpolov_role_id"], config["intolerance_role_id"]}
            )
        if not isinstance(inter.author, disnake.Member) or not any(
            role.id in family_role_ids for role in inter.author.roles
        ):
            return "Подать заявку на отпуск могут только участники с семейной ролью."

        profile = await self.bot.database.get_user(inter.author.id)
        if profile is None:
            return "Ваш профиль отсутствует в базе. Обратитесь к администратору."
        if not profile["is_active"]:
            return (
                f"Ваш профиль неактивен: "
                f"**{profile['inactive_reason'] or 'причина не указана'}**"
            )
        if await self.bot.database.has_pending_leave(inter.author.id):
            return "У вас уже есть запрос на рассмотрении."
        return None

    async def submit_leave_request(
        self,
        inter: disnake.ModalInteraction,
        reason: str,
        days: int,
        evidence: str = "",
    ) -> None:
        error = await self.get_leave_request_error(inter)
        if error is not None:
            await inter.response.send_message(error, ephemeral=True)
            return

        profile = await self.bot.database.get_user(inter.author.id)
        await inter.response.defer(ephemeral=True)
        request = await self.bot.database.create_leave_request(inter.author.id, reason, days)
        channel = self.bot.get_channel(MANAGEMENT_LOG_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(MANAGEMENT_LOG_CHANNEL_ID)
        if not isinstance(channel, (disnake.TextChannel, disnake.Thread)):
            await inter.edit_original_response("Канал заявок недоступен.")
            return

        embed = disnake.Embed(
            title="🏖️ Запрос на отпуск",
            description=f"### {inter.author.mention}\nНовая заявка ожидает решения администрации.",
            color=disnake.Color.gold(),
            timestamp=disnake.utils.utcnow(),
        )
        embed.set_thumbnail(url=inter.author.display_avatar.url)
        embed.add_field(name="🏠 Сервер-источник", value=f"**{inter.guild.name}**\n`{inter.guild_id}`" if inter.guild else "—", inline=False)
        embed.add_field(name="🎮 Игровой ник", value=profile["game_nickname"] or "—", inline=True)
        embed.add_field(name="🌐 Сервер", value=profile["server_name"] or "—", inline=True)
        embed.add_field(name="📆 Срок", value=f"**{days}** дн.\nДо <t:{int(request['ends_at'].timestamp())}:D>", inline=True)
        embed.add_field(name="📝 Причина", value=reason, inline=False)
        embed.add_field(name="🔗 Доказательства", value=evidence or "Не указаны", inline=False)
        embed.add_field(name="🆔 Discord ID", value=f"`{inter.author.id}`", inline=True)
        embed.set_footer(text=f"INTERPOLOV & INTOLERANCE • Заявка #{request['id']}")
        components = [
            disnake.ui.Button(label="Одобрить", emoji="✅", style=disnake.ButtonStyle.success, custom_id=f"leave:approved:{request['id']}"),
            disnake.ui.Button(label="Отклонить", emoji="✖️", style=disnake.ButtonStyle.danger, custom_id=f"leave:declined:{request['id']}"),
        ]
        message = await channel.send(embed=embed, components=components)
        await self.bot.database.set_leave_log_message(request["id"], message.id)
        await inter.edit_original_response(
            f"✅ Заявка **#{request['id']}** отправлена администрации."
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
        if approved:
            await self.set_leave_role(request["discord_id"], enabled=True)
        member = inter.guild.get_member(request["discord_id"]) if inter.guild else None
        if member is None and inter.guild is not None:
            try:
                member = await inter.guild.fetch_member(request["discord_id"])
            except disnake.DiscordException:
                member = None
        if member is not None:
            try:
                await member.send(
                    f"Ваша заявка на отпуск `#{request_id}` "
                    f"**{'одобрена' if approved else 'отклонена'}**.\n"
                    f"Решение принял: **{inter.author}** "
                    f"({inter.author.mention}, `{inter.author.id}`)."
                )
            except disnake.DiscordException:
                logging.warning("Could not send leave decision DM to %s", member.id)
        await send_admin_log(
            self.bot,
            title="✅ Отпуск одобрен" if approved else "❌ Отпуск отклонён",
            description=f"Администратор: {inter.author.mention}\nПользователь: <@{request['discord_id']}>\nЗаявка: `#{request_id}`",
            color=disnake.Color.green() if approved else disnake.Color.red(),
        )
