import asyncio
import logging
import re
from typing import TYPE_CHECKING

import asyncpg
import disnake
from disnake.ext import commands

from access_control import member_access_level
from config import (
    APPLICATION_EXTRA_ACCEPTED_ROLE_IDS,
    APPLICATION_LOG_CHANNEL_ID,
    FAMILY_MEMBER_ROLE_ID,
    FAMILY_NAME,
    GUILD_IDS,
)
from cogs.systems import format_blacklist_matches, parse_static_ids
from log_utils import send_admin_log
from transcript import save_application_json, save_ticket_transcript

if TYPE_CHECKING:
    from bot import IntoleranceBot


STATUS_NAMES = {
    "pending": "Ожидает решения",
    "review": "На рассмотрении",
    "interview": "Вызван на обзвон",
    "accepted": "Принят",
    "declined": "Отклонён",
}
def server_select(family_id: int, server_rows) -> disnake.ui.StringSelect:
    return disnake.ui.StringSelect(
        placeholder="Выберите один или несколько серверов",
        custom_id=f"family_application:server:{family_id}",
        min_values=1,
        max_values=min(len(server_rows), 25),
        options=[
            disnake.SelectOption(label=row["name"], value=str(row["id"]))
            for row in server_rows[:25]
        ],
    )


def application_buttons(application_id: int) -> list[disnake.ui.Button]:
    return [
        disnake.ui.Button(label="Принять", emoji="✅", style=disnake.ButtonStyle.success, custom_id=f"application:accepted:{application_id}"),
        disnake.ui.Button(label="На рассмотрение", emoji="📌", style=disnake.ButtonStyle.primary, custom_id=f"application:review:{application_id}"),
        disnake.ui.Button(label="Вызвать на обзвон", emoji="🎙️", style=disnake.ButtonStyle.primary, custom_id=f"application:interview:{application_id}"),
        disnake.ui.Button(label="Отклонить", emoji="✖️", style=disnake.ButtonStyle.danger, custom_id=f"application:declined:{application_id}"),
    ]


def application_panel() -> tuple[disnake.Embed, list[disnake.ui.Button]]:
    embed = disnake.Embed(
        title=FAMILY_NAME,
        description=(
            "Заполните анкету для вступления в объединённую семью. "
            "После отправки будет создан приватный тикет с рекрутёрами."
        ),
        color=disnake.Color.red(),
    )
    embed.set_footer(text=f"{FAMILY_NAME} • Общий набор")
    return embed, [
        disnake.ui.Button(
            label="Подать заявку",
            style=disnake.ButtonStyle.primary,
            custom_id="family_application:open",
        )
    ]


def application_embed(
    application: asyncpg.Record,
    applicant: disnake.Member,
    previous: list[asyncpg.Record],
) -> disnake.Embed:
    embed = disnake.Embed(
        title=f"📝 Заявление в {application['family_name']} #{application['id']}",
        description=f"Кандидат: {applicant.mention}",
        color=disnake.Color.gold(),
        timestamp=application["created_at"],
    )
    embed.set_thumbnail(url=applicant.display_avatar.url)
    embed.add_field(name="🤝 Выбранная фама", value=f"**{application['family_name']}**", inline=False)
    embed.add_field(name="🎮 Игровые серверы", value=f"**{application['game_server']}**", inline=True)
    embed.add_field(name="🏠 Сервер-источник", value=f"**{applicant.guild.name}**\n`{application['guild_id']}`", inline=False)
    if previous:
        history = "\n".join(
            f"`#{row['id']}` · **{row['family_name']}** — "
            f"**{STATUS_NAMES.get(row['status'], row['status'])}** · "
            f"<t:{int(row['created_at'].timestamp())}:D>"
            for row in previous
        )
    else:
        history = "Заявок не найдено."
    embed.add_field(name="📚 Предыдущие заявки", value=history[:1024], inline=False)
    embed.add_field(name="🎮 Ник в игре", value=application["game_nickname"], inline=True)
    embed.add_field(name="#️⃣ Статик", value=application["static_id"], inline=True)
    embed.add_field(name="🎂 Возраст OOC", value=str(application["ooc_age"]), inline=True)
    embed.add_field(name="🎯 Цель вступления", value=application["goal"], inline=False)
    embed.add_field(name="🖼️ Фото всех персонажей", value=application["screenshot_url"], inline=False)
    embed.add_field(name="👤 Пользователь", value=f"{applicant.mention}\n`{applicant}` · `{applicant.id}`", inline=False)
    embed.add_field(name="📌 Статус", value=STATUS_NAMES[application["status"]], inline=False)
    embed.set_footer(text="INTERPOLOV & INTOLERANCE • Общий набор")
    return embed


class ApplicationModal(disnake.ui.Modal):
    def __init__(
        self, cog: "ApplicationsCog", config: asyncpg.Record, family: asyncpg.Record,
        game_server: str,
    ) -> None:
        self.cog = cog
        self.config = config
        self.family_id = family["id"]
        self.family_name = family["name"]
        self.game_server = game_server
        components = [
            disnake.ui.TextInput(label="Ваш ник в игре", custom_id="game_nickname", max_length=100),
            disnake.ui.TextInput(label="Статик #", custom_id="static_id", max_length=30),
            disnake.ui.TextInput(label="Возраст OOC", custom_id="ooc_age", max_length=2),
            disnake.ui.TextInput(label="Цель вступления", custom_id="goal", style=disnake.TextInputStyle.paragraph, max_length=500),
            disnake.ui.TextInput(label="Фото всех персонажей (ссылкой)", custom_id="screenshot_url", max_length=500),
        ]
        super().__init__(
            title=f"Заявка в {self.family_name}",
            custom_id=f"family_application:modal:{self.family_id}",
            components=components,
            timeout=600,
        )

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        if not isinstance(inter.author, disnake.Member):
            return
        try:
            age = int(inter.text_values["ooc_age"])
        except ValueError:
            await inter.response.send_message("Возраст должен быть указан числом.", ephemeral=True)
            return
        if not 14 <= age <= 99:
            await inter.response.send_message("Укажите корректный возраст от 14 до 99 лет.", ephemeral=True)
            return
        screenshot_url = inter.text_values["screenshot_url"].strip()
        if not screenshot_url.startswith(("https://", "http://")):
            await inter.response.send_message("Ссылка на фото должна начинаться с `https://`.", ephemeral=True)
            return
        if inter.guild_id is None:
            return
        if await self.cog.bot.database.has_open_application(inter.author.id, inter.guild_id):
            await inter.response.send_message("У вас уже есть открытая заявка.", ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)
        guild = inter.guild
        if guild is None:
            return
        category = guild.get_channel(self.config["category_id"])
        staff_roles = await self.cog.application_staff_roles(guild)
        if not isinstance(category, disnake.CategoryChannel) or not staff_roles or guild.me is None:
            await inter.edit_original_response(
                "Настройки тикетов устарели или для уровней доступа 2–4 не назначены "
                "роли. Сообщите администратору."
            )
            return

        safe_name = re.sub(r"[^a-z0-9-]", "", inter.author.name.lower().replace("_", "-"))[:40] or str(inter.author.id)
        overwrites = {
            guild.default_role: disnake.PermissionOverwrite(view_channel=False),
            inter.author: disnake.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: disnake.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        for staff_role in staff_roles:
            overwrites[staff_role] = disnake.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
        try:
            ticket = await category.create_text_channel(
                f"family-{self.family_id}-{safe_name}",
                overwrites=overwrites,
                reason=f"Заявка в {self.family_name} от {inter.author}",
            )
        except disnake.DiscordException:
            logging.exception("Could not create application ticket")
            await inter.edit_original_response("Не удалось создать тикет. Проверьте права бота.")
            return

        application = await self.cog.bot.database.create_application(
            guild.id,
            self.family_name,
            self.game_server,
            inter.author.id,
            str(inter.author),
            inter.text_values["game_nickname"].strip(),
            inter.text_values["static_id"].strip(),
            age,
            inter.text_values["goal"].strip(),
            screenshot_url,
        )
        blacklist_entries = await self.cog.bot.database.find_blacklist_entries(
            inter.author.id, parse_static_ids(application["static_id"])
        )
        if blacklist_entries:
            matched_ids = format_blacklist_matches(blacklist_entries)
            await send_admin_log(
                self.cog.bot,
                title="🚨 Заявка от пользователя из ЧС",
                description=(
                    f"Заявка: `#{application['id']}` в **{self.family_name}**\n"
                    f"Кандидат: {inter.author.mention} (`{inter.author.id}`)\n"
                    f"Статик заявки: `{application['static_id']}`\n"
                    f"Совпавшие записи ЧС: **{matched_ids}**\n\n"
                    "Информация конфиденциальна и не должна распространяться без "
                    "одобрения руководства."
                ),
                color=disnake.Color.red(),
            )
        previous = await self.cog.bot.database.get_previous_applications(inter.author.id, application["id"])
        application_json_path = None
        try:
            application_json_path = await save_application_json(application, previous)
            await self.cog.bot.database.set_application_json_path(
                application["id"], str(application_json_path)
            )
        except (OSError, TypeError, ValueError):
            logging.exception("Could not save application JSON")
        embed = application_embed(application, inter.author, previous)
        message = await ticket.send(
            content=f"{' '.join(role.mention for role in staff_roles)} {inter.author.mention}",
            embed=embed,
            components=application_buttons(application["id"]),
        )
        await self.cog.bot.database.set_application_ticket(application["id"], ticket.id, message.id)
        application_log_channel = self.cog.bot.get_channel(APPLICATION_LOG_CHANNEL_ID)
        if application_log_channel is None:
            try:
                application_log_channel = await self.cog.bot.fetch_channel(APPLICATION_LOG_CHANNEL_ID)
            except disnake.DiscordException:
                logging.exception("Could not fetch application log channel")
        if isinstance(application_log_channel, (disnake.TextChannel, disnake.Thread)):
            try:
                await application_log_channel.send(
                    content=(
                        f"🧾 **Новая заявка в {self.family_name} #{application['id']}**\n"
                        f"Кандидат: {inter.author.mention} · Тикет: {ticket.mention}"
                    ),
                    embed=embed,
                    file=disnake.File(str(application_json_path), filename=f"application-{application['id']}.json") if application_json_path else None,
                )
            except disnake.DiscordException:
                logging.exception("Could not send application log")
        elif application_log_channel is not None:
            logging.error("Application log channel %s is not a text channel", APPLICATION_LOG_CHANNEL_ID)
        await inter.edit_original_response(f"✅ Заявка создана: {ticket.mention}")


class ApplicationsCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild_id in GUILD_IDS:
            await self.refresh_application_panel(guild_id)
            await self._repair_finalized_application_tickets(guild_id)

    async def refresh_application_panel(self, guild_id: int) -> bool:
        config = await self.bot.database.get_application_config(guild_id)
        if config is None or config["panel_message_id"] is None:
            return False
        channel = self.bot.get_channel(config["panel_channel_id"])
        if not isinstance(channel, disnake.TextChannel):
            return False
        try:
            message = await channel.fetch_message(config["panel_message_id"])
            embed, buttons = application_panel()
            await message.edit(embed=embed, components=buttons)
        except disnake.DiscordException:
            logging.exception("Could not refresh application panel in guild %s", guild_id)
            return False
        return True

    async def application_staff_roles(
        self, guild: disnake.Guild
    ) -> list[disnake.Role]:
        role_ids = [
            row["role_id"]
            for row in await self.bot.database.get_access_roles(guild.id)
            if 2 <= row["access_level"] <= 4
        ]
        for department in await self.bot.database.get_department_configs(guild.id):
            role_ids.append(department["head_role_id"])
            if department["department"] == "recruiters":
                role_ids.append(department["member_role_id"])
        return [
            role for role_id in dict.fromkeys(role_ids)
            if (role := guild.get_role(role_id)) is not None
        ]

    async def _repair_finalized_application_tickets(self, guild_id: int) -> None:
        applications = await self.bot.database.get_finalized_ticket_applications(
            guild_id
        )
        for application in applications:
            channel = self.bot.get_channel(application["ticket_channel_id"])
            if not isinstance(channel, disnake.TextChannel):
                continue
            try:
                message = await channel.fetch_message(application["ticket_message_id"])
            except disnake.DiscordException:
                logging.exception(
                    "Could not fetch message while repairing application %s",
                    application["id"],
                )
                continue
            await self._sync_application_message(message, application)
            if application["transcript_path"]:
                await self._ensure_ticket_delete_prompt(
                    channel,
                    application["id"],
                    "✅ Заявка рассмотрена, транскрипт сохранён. "
                    "Теперь тикет можно удалить.",
                )
            else:
                await self._finalize_ticket(
                    channel,
                    application,
                    STATUS_NAMES[application["status"]],
                    "✅ Заявка рассмотрена, транскрипт сохранён. "
                    "Теперь тикет можно удалить.",
                )

    async def _save_ticket_transcript_with_retry(
        self,
        channel: disnake.TextChannel,
        application: asyncpg.Record,
    ):
        application_id = application["id"]
        for attempt in range(1, 4):
            try:
                transcript_path = await save_ticket_transcript(channel, application)
                await self.bot.database.set_application_transcript(
                    application_id, str(transcript_path)
                )
                return transcript_path
            except Exception:
                logging.exception(
                    "Could not save transcript for application %s (attempt %s/3)",
                    application_id,
                    attempt,
                )
                if attempt < 3:
                    await asyncio.sleep(attempt)
        return None

    async def _sync_application_message(
        self,
        message: disnake.Message,
        application: asyncpg.Record,
    ) -> bool:
        status = application["status"]
        if status not in STATUS_NAMES or not message.embeds:
            return False

        embed = message.embeds[0]
        embed.color = {
            "pending": disnake.Color.gold(),
            "accepted": disnake.Color.green(),
            "declined": disnake.Color.red(),
            "review": disnake.Color.blurple(),
            "interview": disnake.Color.gold(),
        }[status]
        reviewer_id = application["reviewer_id"]
        status_value = f"**{STATUS_NAMES[status]}**"
        if reviewer_id is not None:
            status_value += f"\nРекрутёр: <@{reviewer_id}>"
        status_index = next(
            (
                index
                for index, field in enumerate(embed.fields)
                if field.name == "📌 Статус"
            ),
            None,
        )
        if status_index is None:
            embed.add_field(name="📌 Статус", value=status_value, inline=False)
        else:
            embed.set_field_at(
                status_index,
                name="📌 Статус",
                value=status_value,
                inline=False,
            )

        final = status in {"accepted", "declined"}
        try:
            await message.edit(
                embed=embed,
                components=[] if final else application_buttons(application["id"]),
            )
        except disnake.DiscordException:
            logging.exception(
                "Could not synchronize message for application %s",
                application["id"],
            )
            return False
        return True

    async def _ensure_ticket_delete_prompt(
        self,
        channel: disnake.TextChannel,
        application_id: int,
        content: str,
    ) -> bool:
        marker = f"application:delete:{application_id}"
        try:
            async for message in channel.history(limit=20):
                if message.author == self.bot.user and any(
                    getattr(component, "custom_id", None) == marker
                    for row in message.components
                    for component in getattr(row, "children", [])
                ):
                    return True
        except disnake.DiscordException:
            logging.exception(
                "Could not check the delete prompt for application %s",
                application_id,
            )

        delete_button = disnake.ui.Button(
            label="Удалить тикет",
            emoji="🗑️",
            style=disnake.ButtonStyle.danger,
            custom_id=marker,
        )
        for attempt in range(1, 4):
            try:
                await channel.send(content=content, components=[delete_button])
                return True
            except disnake.DiscordException:
                logging.exception(
                    "Could not send the delete prompt for application %s (attempt %s/3)",
                    application_id,
                    attempt,
                )
                if attempt < 3:
                    await asyncio.sleep(attempt)
        return False

    async def _upload_ticket_transcript(
        self,
        channel: disnake.TextChannel,
        application_id: int,
        status_text: str,
        transcript_path,
    ) -> None:
        application_log_channel = self.bot.get_channel(APPLICATION_LOG_CHANNEL_ID)
        if application_log_channel is None:
            try:
                application_log_channel = await self.bot.fetch_channel(
                    APPLICATION_LOG_CHANNEL_ID
                )
            except disnake.DiscordException:
                logging.exception("Could not fetch application log channel")
                return
        if not isinstance(
            application_log_channel, (disnake.TextChannel, disnake.Thread)
        ):
            logging.error(
                "Application log channel %s is not a text channel",
                APPLICATION_LOG_CHANNEL_ID,
            )
            return
        try:
            await application_log_channel.send(
                content=(
                    f"📚 **Транскрипт заявки #{application_id}** · "
                    f"{status_text} · тикет {channel.mention}"
                ),
                file=disnake.File(
                    str(transcript_path),
                    filename=f"application-{application_id}.json",
                ),
            )
        except disnake.DiscordException:
            logging.exception(
                "Could not upload transcript for application %s", application_id
            )

    async def _finalize_ticket(
        self,
        channel: disnake.TextChannel,
        application: asyncpg.Record,
        status_text: str,
        prompt_text: str,
    ) -> None:
        application_id = application["id"]
        transcript_path = await self._save_ticket_transcript_with_retry(
            channel, application
        )
        if transcript_path is None:
            try:
                await channel.send(
                    "⚠️ Не удалось сохранить транскрипт после трёх попыток. "
                    "Тикет пока нельзя удалить; сообщите администратору."
                )
            except disnake.DiscordException:
                logging.exception(
                    "Could not report transcript failure for application %s",
                    application_id,
                )
            return

        await self._ensure_ticket_delete_prompt(
            channel, application_id, prompt_text
        )
        await self._upload_ticket_transcript(
            channel, application_id, status_text, transcript_path
        )

    async def _decline_departed_member_application(
        self, member: disnake.Member, application: asyncpg.Record
    ) -> None:
        application_id = application["id"]
        channel = self.bot.get_channel(application["ticket_channel_id"])
        if channel is None and application["ticket_channel_id"] is not None:
            try:
                channel = await self.bot.fetch_channel(application["ticket_channel_id"])
            except disnake.DiscordException:
                logging.exception(
                    "Could not fetch ticket channel for automatically declined application %s",
                    application_id,
                )

        if isinstance(channel, disnake.TextChannel):
            if application["ticket_message_id"] is not None:
                try:
                    message = await channel.fetch_message(application["ticket_message_id"])
                    if message.embeds:
                        embed = message.embeds[0]
                        embed.color = disnake.Color.red()
                        status_index = next(
                            (
                                index
                                for index, field in enumerate(embed.fields)
                                if field.name == "📌 Статус"
                            ),
                            None,
                        )
                        status_value = (
                            "**Отклонён**\nАвтоматически: пользователь покинул сервер"
                        )
                        if status_index is None:
                            embed.add_field(
                                name="📌 Статус", value=status_value, inline=False
                            )
                        else:
                            embed.set_field_at(
                                status_index,
                                name="📌 Статус",
                                value=status_value,
                                inline=False,
                            )
                        await message.edit(embed=embed, components=[])
                    else:
                        await message.edit(components=[])
                except disnake.DiscordException:
                    logging.exception(
                        "Could not update ticket message for automatically declined application %s",
                        application_id,
                    )

            try:
                await channel.send(
                    f"❌ Заявка **#{application_id}** автоматически отклонена: "
                    "пользователь покинул сервер."
                )
            except disnake.DiscordException:
                logging.exception(
                    "Could not announce automatically declined application %s",
                    application_id,
                )
            await self._finalize_ticket(
                channel,
                application,
                "Отклонён автоматически",
                "✅ Заявка отклонена, транскрипт сохранён. Теперь тикет можно удалить.",
            )

        await send_admin_log(
            self.bot,
            title=f"🧾 Заявка #{application_id}: Отклонён автоматически",
            description=(
                f"Кандидат: {member} (`{member.id}`)\n"
                "Причина: **пользователь покинул сервер**"
            ),
            color=disnake.Color.red(),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: disnake.Member) -> None:
        if member.guild.id not in GUILD_IDS or self.bot.user is None:
            return
        applications = (
            await self.bot.database.decline_open_applications_for_departed_member(
                member.id, member.guild.id, self.bot.user.id
            )
        )
        for application in applications:
            await self._decline_departed_member_application(member, application)

    @commands.slash_command(
        name="applications",
        description="Настройка заявок в семью",
        guild_ids=GUILD_IDS,
    )
    async def applications(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @commands.Cog.listener("on_dropdown")
    async def application_server_select(
        self, inter: disnake.MessageInteraction
    ) -> None:
        custom_id = inter.component.custom_id or ""
        if not custom_id.startswith("family_application:server:"):
            return
        if inter.guild_id is None:
            return

        try:
            family_id = int(custom_id.rsplit(":", 1)[-1])
            server_ids = {int(value) for value in inter.values}
        except ValueError:
            await inter.response.send_message("Некорректный выбор.", ephemeral=True)
            return

        family = await self.bot.database.get_family(inter.guild_id, family_id)
        server_rows = [
            row for row in await self.bot.database.get_game_servers(inter.guild_id)
            if row["role_id"] is not None and inter.guild is not None
            and inter.guild.get_role(row["role_id"]) is not None
        ]
        chosen_servers = [row for row in server_rows if row["id"] in server_ids]
        if family is None or not chosen_servers or len(chosen_servers) != len(server_ids):
            await inter.response.send_message("Некорректный или устаревший выбор.", ephemeral=True)
            return

        config = await self.bot.database.get_application_config(inter.guild_id)
        if config is None:
            await inter.response.send_message("Система заявок ещё не настроена.", ephemeral=True)
            return
        if await self.bot.database.has_open_application(inter.author.id, inter.guild_id):
            await inter.response.send_message("У вас уже есть открытая заявка.", ephemeral=True)
            return

        await inter.response.send_modal(
            ApplicationModal(
                self,
                config,
                family,
                ", ".join(row["name"] for row in chosen_servers),
            )
        )

    @applications.sub_command(name="setup", description="Создать панель заявок")
    async def applications_setup(
        self,
        inter: disnake.ApplicationCommandInteraction,
        panel_channel: disnake.TextChannel = commands.Param(description="Канал с кнопкой подачи заявки"),
        ticket_category: disnake.CategoryChannel = commands.Param(description="Категория приватных тикетов"),
    ) -> None:
        if inter.guild_id is None or inter.guild is None:
            return
        family_role = inter.guild.get_role(FAMILY_MEMBER_ROLE_ID)
        if family_role is None:
            await inter.response.send_message(
                "Роль объединённой семьи не найдена на сервере.", ephemeral=True
            )
            return
        await inter.response.defer(ephemeral=True)
        try:
            await self.bot.database.save_application_config(
                inter.guild_id,
                panel_channel.id,
                ticket_category.id,
                family_role.id,
            )
            await self.bot.database.upsert_family(
                inter.guild_id, FAMILY_NAME, family_role.id, inter.author.id
            )
        except asyncpg.PostgresError:
            logging.exception("Could not save application configuration")
            await inter.edit_original_response(
                "❌ Не удалось сохранить настройки заявок в базе данных."
            )
            return
        embed, buttons = application_panel()
        try:
            message = await panel_channel.send(embed=embed, components=buttons)
        except disnake.DiscordException:
            logging.exception("Could not send application panel")
            await inter.edit_original_response(
                f"❌ Не удалось отправить панель в {panel_channel.mention}. "
                "Проверьте право бота **Отправлять сообщения** в этом канале."
            )
            return
        await self.bot.database.set_application_panel_message(inter.guild_id, message.id)
        await inter.edit_original_response(f"✅ Панель создана в {panel_channel.mention}.")
        await send_admin_log(
            self.bot,
            title="🧾 Панель заявок настроена",
            description=(
                f"Администратор: {inter.author.mention}\n"
                f"Канал: {panel_channel.mention}\n"
                f"Категория: **{ticket_category.name}**\n"
                "Доступ к тикетам: **уровни 2–4**\n"
                f"Объединённая семья: {family_role.mention}"
            ),
            color=disnake.Color.green(),
        )

    @commands.Cog.listener("on_button_click")
    async def application_button(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id or ""
        if custom_id == "family_application:open" or custom_id.startswith("family_application:open:"):
            if inter.guild_id is None:
                return
            family = await self.bot.database.get_family_by_name(
                inter.guild_id, FAMILY_NAME
            )
            server_rows = [
                row for row in await self.bot.database.get_game_servers(inter.guild_id)
                if row["role_id"] is not None and inter.guild is not None
                and inter.guild.get_role(row["role_id"]) is not None
            ]
            if family is None or not server_rows:
                await inter.response.send_message(
                    "Список семей или игровых серверов пуст/устарел.", ephemeral=True
                )
                return
            config = await self.bot.database.get_application_config(inter.guild_id)
            if config is None:
                await inter.response.send_message("Система заявок ещё не настроена.", ephemeral=True)
                return
            if await self.bot.database.has_open_application(inter.author.id, inter.guild_id):
                await inter.response.send_message("У вас уже есть открытая заявка.", ephemeral=True)
                return
            await inter.response.send_message(
                "Выберите один или несколько серверов, на которых вы играете:",
                components=server_select(family["id"], server_rows),
                ephemeral=True,
            )
            return
        if not custom_id.startswith("application:"):
            return

        if not isinstance(inter.author, disnake.Member):
            return
        if inter.guild_id is None:
            return
        try:
            _, status, raw_id = custom_id.split(":", 2)
            application_id = int(raw_id)
        except ValueError:
            return

        if status not in {"accepted", "review", "interview", "declined", "delete"}:
            return

        await inter.response.defer()
        config = await self.bot.database.get_application_config(inter.guild_id)
        if await member_access_level(self.bot, inter.author) < 2:
            await inter.followup.send(
                "Только рекрутёры могут обрабатывать заявки.", ephemeral=True
            )
            return

        if status == "delete":
            application = await self.bot.database.get_application(application_id)
            if application is None or not application["transcript_path"]:
                await inter.followup.send(
                    "Удаление недоступно: транскрипт ещё не сохранён.", ephemeral=True
                )
                return
            await inter.followup.send(
                "🗑️ Тикет будет удалён через секунду.", ephemeral=True
            )
            await send_admin_log(
                self.bot,
                title=f"🗑️ Тикет заявки #{application_id} удалён",
                description=f"Рекрутёр: {inter.author.mention}\nКанал: {inter.channel.mention}",
                color=disnake.Color.red(),
            )
            await asyncio.sleep(1)
            if isinstance(inter.channel, disnake.TextChannel):
                await inter.channel.delete(
                    reason=f"Транскрипт заявки #{application_id} сохранён; удалил {inter.author}"
                )
            return

        current_application = await self.bot.database.get_application(application_id)
        if current_application is None:
            await inter.followup.send("Заявка не найдена.", ephemeral=True)
            return
        if current_application["status"] in {"accepted", "declined"}:
            await self._sync_application_message(inter.message, current_application)
            if (
                not current_application["transcript_path"]
                and isinstance(inter.channel, disnake.TextChannel)
            ):
                await self._finalize_ticket(
                    inter.channel,
                    current_application,
                    STATUS_NAMES[current_application["status"]],
                    "✅ Заявка рассмотрена, транскрипт сохранён. "
                    "Теперь тикет можно удалить.",
                )
            await inter.followup.send(
                "Статус сообщения восстановлен из базы: "
                f"**{STATUS_NAMES[current_application['status']]}**.",
                ephemeral=True,
            )
            return
        if status != "declined":
            blacklist_entries = await self.bot.database.find_blacklist_entries(
                current_application["discord_id"],
                parse_static_ids(current_application["static_id"]),
            )
            if blacklist_entries:
                matched_ids = format_blacklist_matches(blacklist_entries)
                await inter.followup.send(
                    "⛔ Пользователь находится в чёрном списке семьи "
                    f"(записи: **{matched_ids}**). Это действие заблокировано; "
                    "доступна только кнопка отказа.\n\n"
                    "⚠️ Информацию о чёрном списке запрещено распространять без "
                    "одобрения руководства.",
                    ephemeral=True,
                )
                return

        applicant = (
            inter.guild.get_member(current_application["discord_id"])
            if inter.guild else None
        )
        if applicant is None and inter.guild is not None:
            try:
                applicant = await inter.guild.fetch_member(
                    current_application["discord_id"]
                )
            except disnake.DiscordException:
                logging.warning(
                    "Could not fetch applicant %s for application %s",
                    current_application["discord_id"], application_id,
                )

        accepted_role: disnake.Role | None = None
        if status == "accepted":
            if applicant is None:
                await inter.followup.send(
                    "❌ Не удалось найти кандидата на сервере. Заявка не была принята.",
                    ephemeral=True,
                )
                return
            if config is None or inter.guild is None or inter.guild.me is None:
                await inter.followup.send(
                    "❌ Настройки заявок недоступны. Заявка не была принята.",
                    ephemeral=True,
                )
                return

            family = await self.bot.database.get_family_by_name(
                inter.guild_id, FAMILY_NAME
            )
            family_role_id = family["role_id"] if family is not None else None
            accepted_role = inter.guild.get_role(family_role_id) if family_role_id else None
            if accepted_role is None:
                await inter.followup.send(
                    "❌ Роль выбранной семьи не найдена. Проверьте настройки семьи. "
                    "Заявка оставлена открытой.",
                    ephemeral=True,
                )
                return

            extra_accepted_roles = [
                role
                for role_id in APPLICATION_EXTRA_ACCEPTED_ROLE_IDS
                if (role := inter.guild.get_role(role_id)) is not None
            ]
            missing_extra_role_ids = APPLICATION_EXTRA_ACCEPTED_ROLE_IDS - {
                role.id for role in extra_accepted_roles
            }
            if missing_extra_role_ids:
                await inter.followup.send(
                    "❌ Не найдены дополнительные роли участника: "
                    f"**{', '.join(map(str, sorted(missing_extra_role_ids)))}**. "
                    "Заявка оставлена открытой.",
                    ephemeral=True,
                )
                return

            selected_servers = {
                server.strip()
                for server in current_application["game_server"].split(",")
            }
            configured_servers = await self.bot.database.get_game_servers(inter.guild_id)
            server_roles_by_name = {
                row["name"]: role
                for row in configured_servers
                if row["role_id"] is not None
                and (role := inter.guild.get_role(row["role_id"])) is not None
            }
            missing_servers = selected_servers - server_roles_by_name.keys()
            if missing_servers:
                await inter.followup.send(
                    "❌ Не найдены роли игровых серверов: "
                    f"**{', '.join(sorted(missing_servers))}**. "
                    "Заявка оставлена открытой.",
                    ephemeral=True,
                )
                return

            roles_to_add = list(
                dict.fromkeys(
                    [accepted_role]
                    + extra_accepted_roles
                    + [server_roles_by_name[name] for name in selected_servers]
                )
            )
            missing_roles = [role for role in roles_to_add if role not in applicant.roles]
            bot_member = inter.guild.me
            unavailable_roles = [
                role
                for role in missing_roles
                if role.managed or role >= bot_member.top_role
            ]
            if not bot_member.guild_permissions.manage_roles or unavailable_roles:
                details = (
                    ", ".join(role.mention for role in unavailable_roles)
                    if unavailable_roles else "у бота нет права **Управлять ролями**"
                )
                await inter.followup.send(
                    "❌ Бот не может выдать роли: "
                    f"{details}. Поднимите роль бота выше выдаваемых ролей и "
                    "проверьте право **Управлять ролями**. Заявка оставлена открытой.",
                    ephemeral=True,
                )
                return

            if missing_roles:
                try:
                    await applicant.add_roles(
                        *missing_roles, reason=f"Заявка #{application_id} принята"
                    )
                    applicant = await inter.guild.fetch_member(applicant.id)
                except disnake.DiscordException as error:
                    logging.exception("Could not add accepted family and server roles")
                    await inter.followup.send(
                        "❌ Discord отклонил выдачу ролей. Проверьте права и иерархию "
                        f"ролей бота. Заявка оставлена открытой. (`{type(error).__name__}`)",
                        ephemeral=True,
                    )
                    return

            roles_not_added = [role for role in roles_to_add if role not in applicant.roles]
            if roles_not_added:
                await inter.followup.send(
                    "❌ Не удалось подтвердить выдачу ролей: "
                    f"{', '.join(role.mention for role in roles_not_added)}. "
                    "Заявка оставлена открытой.",
                    ephemeral=True,
                )
                return

        application = await self.bot.database.review_application(
            application_id, status, inter.author.id
        )
        if application is None:
            latest_application = await self.bot.database.get_application(application_id)
            if latest_application is not None:
                await self._sync_application_message(inter.message, latest_application)
                latest_status = STATUS_NAMES.get(
                    latest_application["status"], latest_application["status"]
                )
                await inter.followup.send(
                    f"Заявка уже обработана. Текущий статус: **{latest_status}**.",
                    ephemeral=True,
                )
            else:
                await inter.followup.send("Заявка не найдена.", ephemeral=True)
            return

        if status == "accepted" and applicant is not None and accepted_role is not None:
            if accepted_role is not None and accepted_role in applicant.roles:
                await self.bot.database.save_users([applicant], role_ids={accepted_role.id})
                profile = await self.bot.database.get_user(applicant.id)
                if profile is not None:
                    await self.bot.database.update_user_profile(
                        applicant.id,
                        game_nickname=application["game_nickname"],
                        server_name=application["game_server"],
                        added_by=inter.author.id,
                        reactivate=True,
                    )
                    await self.bot.database.set_user_family(
                        applicant.id, FAMILY_NAME
                    )
                    self.bot.dispatch("voice_eligibility_changed")
            else:
                logging.error(
                    "Accepted applicant %s was not saved: family role is missing",
                    applicant.id,
                )

        final = status in {"accepted", "declined"}
        message_synchronized = await self._sync_application_message(
            inter.message, application
        )
        if not message_synchronized:
            await inter.followup.send(
                "⚠️ Статус в базе обновлён, но сообщение заявки обновить не удалось. "
                "Повторное нажатие синхронизирует сообщение.",
                ephemeral=True,
            )
        if applicant is not None:
            try:
                await applicant.send(
                    f"Статус вашей заявки в **{application['family_name']}** "
                    f"**#{application_id}** изменён: **{STATUS_NAMES[status]}**.\n"
                    f"Решение принял: **{inter.author}** "
                    f"({inter.author.mention}, `{inter.author.id}`)."
                )
            except disnake.DiscordException:
                pass
        if final and isinstance(inter.channel, disnake.TextChannel):
            await self._finalize_ticket(
                inter.channel,
                application,
                STATUS_NAMES[status],
                "✅ Заявка рассмотрена, транскрипт сохранён. Теперь тикет можно удалить.",
            )
        await send_admin_log(
            self.bot,
            title=f"🧾 Заявка #{application_id}: {STATUS_NAMES[status]}",
            description=f"Рекрутёр: {inter.author.mention}\nКандидат: <@{application['discord_id']}> (`{application['discord_id']}`)",
            color={
                "accepted": disnake.Color.green(),
                "declined": disnake.Color.red(),
                "review": disnake.Color.blurple(),
                "interview": disnake.Color.gold(),
            }[status],
        )
