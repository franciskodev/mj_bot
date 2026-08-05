import logging
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from config import (
    APPEAL_LOG_CHANNEL_ID,
    GUILD_IDS,
    MANAGEMENT_LOG_CHANNEL_ID,
    PROMOTION_LOG_CHANNEL_ID,
    WARNING_WORKOFF_LOG_CHANNEL_ID,
)

if TYPE_CHECKING:
    from bot import IntoleranceBot
    from cogs.leave import LeaveCog


def optional_value(value: str) -> str:
    return value.strip() or "Не указаны"


class PromotionModal(disnake.ui.Modal):
    def __init__(self, cog: "ManagementCog") -> None:
        self.cog = cog
        super().__init__(
            title="Заявка на повышение",
            custom_id="management:promotion:modal",
            components=[
                disnake.ui.TextInput(
                    label="Что вы сделали",
                    custom_id="achievements",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=1500,
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
        achievements = inter.text_values["achievements"].strip()
        if not achievements:
            await inter.response.send_message(
                "Опишите, что вы сделали для повышения.", ephemeral=True
            )
            return
        embed = self.cog.request_embed(inter, "📈 Заявка на повышение")
        embed.add_field(name="Что сделано", value=achievements, inline=False)
        embed.add_field(
            name="Доказательства",
            value=optional_value(inter.text_values["evidence"]),
            inline=False,
        )
        await self.cog.send_request(
            inter,
            embed,
            channel_id=PROMOTION_LOG_CHANNEL_ID,
        )


class AppealModal(disnake.ui.Modal):
    def __init__(self, cog: "ManagementCog", warning_id: int) -> None:
        self.cog = cog
        self.warning_id = warning_id
        super().__init__(
            title=f"Обжалование выговора #{warning_id}",
            custom_id=f"management:appeal:modal:{warning_id}",
            components=[
                disnake.ui.TextInput(
                    label="Причина обжалования",
                    custom_id="reason",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=1500,
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
        warnings = await self.cog.bot.database.get_disciplinary_warnings(inter.author.id)
        warning = next(
            (
                row
                for row in warnings
                if row["id"] == self.warning_id and row["revoked_at"] is None
            ),
            None,
        )
        if warning is None:
            await inter.response.send_message(
                "Этот выговор уже снят или не найден.", ephemeral=True
            )
            return

        reason = inter.text_values["reason"].strip()
        if not reason:
            await inter.response.send_message(
                "Укажите причину обжалования.", ephemeral=True
            )
            return

        embed = self.cog.request_embed(inter, "⚖️ Обжалование выговора")
        embed.add_field(
            name="Выговор",
            value=f"`#{self.warning_id}` — {warning['reason']}",
            inline=False,
        )
        embed.add_field(name="Причина обжалования", value=reason, inline=False)
        embed.add_field(
            name="Доказательства",
            value=optional_value(inter.text_values["evidence"]),
            inline=False,
        )
        await self.cog.send_warning_request(
            inter,
            embed,
            warning_id=self.warning_id,
            request_type="appeal",
        )


class WarningWorkoffModal(disnake.ui.Modal):
    def __init__(self, cog: "ManagementCog", warning_id: int) -> None:
        self.cog = cog
        self.warning_id = warning_id
        super().__init__(
            title=f"Отработка выговора #{warning_id}",
            custom_id=f"management:workoff:modal:{warning_id}",
            components=[
                disnake.ui.TextInput(
                    label="Что вы сделали для отработки",
                    custom_id="work_done",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=1500,
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
        warning = await self.cog.get_active_warning(inter.author.id, self.warning_id)
        if warning is None:
            await inter.response.send_message(
                "Этот выговор уже снят или не найден.", ephemeral=True
            )
            return

        work_done = inter.text_values["work_done"].strip()
        if not work_done:
            await inter.response.send_message(
                "Опишите выполненную отработку.", ephemeral=True
            )
            return

        embed = self.cog.request_embed(inter, "🛠️ Отработка выговора")
        embed.add_field(
            name="Выговор",
            value=f"`#{self.warning_id}` — {warning['reason']}",
            inline=False,
        )
        embed.add_field(name="Выполненная отработка", value=work_done, inline=False)
        embed.add_field(
            name="Доказательства",
            value=optional_value(inter.text_values["evidence"]),
            inline=False,
        )
        await self.cog.send_warning_request(
            inter,
            embed,
            warning_id=self.warning_id,
            request_type="workoff",
        )


class ManagementCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot

    def request_embed(
        self,
        inter: disnake.ModalInteraction,
        title: str,
    ) -> disnake.Embed:
        embed = disnake.Embed(
            title=title,
            description=f"Заявитель: {inter.author.mention} (`{inter.author.id}`)",
            color=disnake.Color.blurple(),
            timestamp=disnake.utils.utcnow(),
        )
        embed.set_thumbnail(url=inter.author.display_avatar.url)
        embed.set_footer(text="INTERPOLOV & INTOLERANCE • Управление")
        return embed

    async def get_active_warning(self, discord_id: int, warning_id: int):
        warnings = await self.bot.database.get_disciplinary_warnings(discord_id)
        return next(
            (
                row
                for row in warnings
                if row["id"] == warning_id and row["revoked_at"] is None
            ),
            None,
        )

    async def send_request(
        self,
        inter: disnake.ModalInteraction,
        embed: disnake.Embed,
        *,
        channel_id: int = MANAGEMENT_LOG_CHANNEL_ID,
    ) -> None:
        await inter.response.defer(ephemeral=True)
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, (disnake.TextChannel, disnake.Thread)):
            await inter.edit_original_response("Канал заявок недоступен.")
            return
        await channel.send(embed=embed)
        await inter.edit_original_response("✅ Заявка отправлена администрации.")

    async def send_warning_request(
        self,
        inter: disnake.ModalInteraction,
        embed: disnake.Embed,
        *,
        warning_id: int,
        request_type: str,
    ) -> None:
        await inter.response.defer(ephemeral=True)
        channel_id = (
            APPEAL_LOG_CHANNEL_ID
            if request_type == "appeal"
            else WARNING_WORKOFF_LOG_CHANNEL_ID
        )
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, (disnake.TextChannel, disnake.Thread)):
            await inter.edit_original_response("Канал заявок недоступен.")
            return

        components = [
            disnake.ui.Button(
                label="Одобрить снятие",
                emoji="✅",
                style=disnake.ButtonStyle.success,
                custom_id=(
                    f"management:warning_review:{request_type}:approve:"
                    f"{warning_id}:{inter.author.id}"
                ),
            ),
            disnake.ui.Button(
                label="Отказать",
                emoji="✖️",
                style=disnake.ButtonStyle.danger,
                custom_id=(
                    f"management:warning_review:{request_type}:decline:"
                    f"{warning_id}:{inter.author.id}"
                ),
            ),
        ]
        await channel.send(embed=embed, components=components)
        await inter.edit_original_response("✅ Заявка отправлена администрации.")

    @commands.slash_command(
        name="управление",
        description="Опубликовать панель управления",
        guild_ids=GUILD_IDS,
    )
    async def management_panel(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ) -> None:
        await inter.response.send_message(
            "Публикую панель управления…",
            ephemeral=True,
        )
        embed = disnake.Embed(
            title="СООБЩЕНИЕ управление",
            description=(
                "Выберите нужное действие ниже:\n\n"
                "🏖️ **Отпуск** — подать заявку на отпуск.\n"
                "📈 **Повышение** — подать заявку на повышение.\n"
                "⚖️ **Обжалование** — обжаловать активный выговор.\n"
                "🛠️ **Отработка выговоров** — отправить выполненную отработку."
            ),
            color=disnake.Color.blurple(),
        )
        components = [
            disnake.ui.Button(
                label="Отпуск",
                emoji="🏖️",
                style=disnake.ButtonStyle.primary,
                custom_id="management:leave",
            ),
            disnake.ui.Button(
                label="Повышение",
                emoji="📈",
                style=disnake.ButtonStyle.success,
                custom_id="management:promotion",
            ),
            disnake.ui.Button(
                label="Обжалование",
                emoji="⚖️",
                style=disnake.ButtonStyle.secondary,
                custom_id="management:appeal",
            ),
            disnake.ui.Button(
                label="Отработка выговоров",
                emoji="🛠️",
                style=disnake.ButtonStyle.secondary,
                custom_id="management:workoff",
            ),
        ]
        if not isinstance(inter.channel, (disnake.TextChannel, disnake.Thread)):
            await inter.edit_original_response(
                "Команду можно использовать только в текстовом канале."
            )
            return
        try:
            await inter.channel.send(embed=embed, components=components)
        except disnake.Forbidden:
            await inter.edit_original_response(
                "Не удалось опубликовать панель: у бота нет права отправлять сообщения "
                "или использовать внешние эмодзи в этом канале."
            )
            return
        except disnake.DiscordException:
            await inter.edit_original_response(
                "Не удалось опубликовать панель из-за ошибки Discord."
            )
            return
        await inter.edit_original_response("✅ Панель опубликована.")

    @commands.Cog.listener("on_button_click")
    async def management_button(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id
        if custom_id == "management:leave":
            leave_cog: "LeaveCog | None" = self.bot.get_cog("LeaveCog")
            if leave_cog is None:
                await inter.response.send_message(
                    "Система отпусков временно недоступна.", ephemeral=True
                )
                return
            error = await leave_cog.get_leave_request_error(inter)
            if error is not None:
                await inter.response.send_message(error, ephemeral=True)
                return
            from cogs.leave import LeaveRequestModal

            await inter.response.send_modal(LeaveRequestModal(leave_cog))
            return

        if custom_id == "management:promotion":
            await inter.response.send_modal(PromotionModal(self))
            return

        if custom_id not in {"management:appeal", "management:workoff"}:
            return

        await inter.response.defer(with_message=True, ephemeral=True)
        try:
            warnings = await self.bot.database.get_disciplinary_warnings(inter.author.id)
        except Exception:
            logging.exception("Could not load warnings for appeal by %s", inter.author.id)
            await inter.edit_original_response(
                "Не удалось загрузить выговоры. Попробуйте ещё раз позднее."
            )
            return
        active_warnings = [row for row in warnings if row["revoked_at"] is None][:25]
        if not active_warnings:
            await inter.edit_original_response(
                "У вас нет активных выговоров для обжалования."
            )
            return
        options = [
            disnake.SelectOption(
                label=f"Выговор #{row['id']}",
                value=str(row["id"]),
                description=row["reason"][:100],
                emoji="⚠️",
            )
            for row in active_warnings
        ]
        select = disnake.ui.StringSelect(
            placeholder=(
                "Выберите выговор для обжалования"
                if custom_id == "management:appeal"
                else "Выберите выговор для отработки"
            ),
            custom_id=(
                "management:appeal:select"
                if custom_id == "management:appeal"
                else "management:workoff:select"
            ),
            options=options,
        )
        await inter.edit_original_response(
            content=(
                "Какой выговор вы хотите обжаловать?"
                if custom_id == "management:appeal"
                else "Какой выговор вы хотите отработать?"
            ),
            components=[select],
        )

    @commands.Cog.listener("on_dropdown")
    async def appeal_select(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id
        if custom_id not in {
            "management:appeal:select",
            "management:workoff:select",
        }:
            return
        try:
            warning_id = int(inter.values[0])
        except (ValueError, IndexError):
            await inter.response.send_message("Не удалось выбрать выговор.", ephemeral=True)
            return
        modal = (
            AppealModal(self, warning_id)
            if custom_id == "management:appeal:select"
            else WarningWorkoffModal(self, warning_id)
        )
        await inter.response.send_modal(modal)

    @commands.Cog.listener("on_button_click")
    async def warning_review_button(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id
        if not custom_id.startswith("management:warning_review:"):
            return
        if (
            not isinstance(inter.author, disnake.Member)
            or not inter.author.guild_permissions.administrator
        ):
            await inter.response.send_message(
                "Только администратор может принять решение.", ephemeral=True
            )
            return

        try:
            _, _, request_type, decision, raw_warning_id, raw_user_id = custom_id.split(":")
            warning_id = int(raw_warning_id)
            user_id = int(raw_user_id)
        except (ValueError, AttributeError):
            await inter.response.send_message("Некорректная заявка.", ephemeral=True)
            return

        await inter.response.defer(with_message=True, ephemeral=True)
        approved = decision == "approve"
        if approved:
            warning, count = await self.bot.database.revoke_disciplinary_warning(
                warning_id,
                user_id,
                inter.author.id,
            )
            if warning is None:
                await inter.message.edit(components=[])
                await inter.edit_original_response(
                    "Этот выговор уже снят или не найден."
                )
                return

        embed = inter.message.embeds[0]
        embed.color = disnake.Color.green() if approved else disnake.Color.red()
        embed.add_field(
            name="Решение администрации",
            value=(
                f"{'✅ Снятие одобрено' if approved else '❌ Отказано'}\n"
                f"Администратор: {inter.author.mention}\n"
                f"<t:{int(disnake.utils.utcnow().timestamp())}:f>"
            ),
            inline=False,
        )
        await inter.message.edit(embed=embed, components=[])
        await inter.edit_original_response("✅ Решение сохранено.")

        member = inter.guild.get_member(user_id) if inter.guild else None
        if member is None and inter.guild is not None:
            try:
                member = await inter.guild.fetch_member(user_id)
            except disnake.DiscordException:
                member = None
        if member is not None:
            request_name = "обжалованию" if request_type == "appeal" else "отработке"
            try:
                await member.send(
                    f"По вашему {request_name} выговора `#{warning_id}` принято решение: "
                    f"**{'снятие одобрено' if approved else 'отказано'}**.\n"
                    f"Решение принял: **{inter.author}** "
                    f"({inter.author.mention}, `{inter.author.id}`)."
                )
            except disnake.DiscordException:
                logging.warning("Could not send warning review result to %s", user_id)
