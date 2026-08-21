import asyncio
import logging
import re
from typing import TYPE_CHECKING

import asyncpg
import disnake
from disnake.ext import commands

from access_control import member_access_level
from config import APPLICATION_LOG_CHANNEL_ID, GUILD_IDS, SERVER_ROLE_IDS
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
FAMILY_NAMES = {
    "interpolov": "INTERPOLOV",
    "intolerance": "INTOLERANCE",
}
GAME_SERVERS = {
    "seattle": "Seattle",
    "orlando": "Orlando",
    "denver": "Denver",
}
def server_select(family_key: str) -> disnake.ui.StringSelect:
    return disnake.ui.StringSelect(
        placeholder="Выберите один или несколько серверов",
        custom_id=f"family_application:server:{family_key}",
        min_values=1,
        max_values=len(GAME_SERVERS),
        options=[
            disnake.SelectOption(label=server, value=key)
            for key, server in GAME_SERVERS.items()
        ],
    )


def application_buttons(application_id: int) -> list[disnake.ui.Button]:
    return [
        disnake.ui.Button(label="Принять", emoji="✅", style=disnake.ButtonStyle.success, custom_id=f"application:accepted:{application_id}"),
        disnake.ui.Button(label="На рассмотрение", emoji="📌", style=disnake.ButtonStyle.primary, custom_id=f"application:review:{application_id}"),
        disnake.ui.Button(label="Вызвать на обзвон", emoji="🎙️", style=disnake.ButtonStyle.primary, custom_id=f"application:interview:{application_id}"),
        disnake.ui.Button(label="Отклонить", emoji="✖️", style=disnake.ButtonStyle.danger, custom_id=f"application:declined:{application_id}"),
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
        self, cog: "ApplicationsCog", config: asyncpg.Record, family_key: str,
        game_server: str,
    ) -> None:
        self.cog = cog
        self.config = config
        self.family_key = family_key
        self.family_name = FAMILY_NAMES[family_key]
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
            custom_id=f"family_application:modal:{family_key}",
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
                f"{self.family_key}-{safe_name}",
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
                transcript_path = await save_ticket_transcript(channel, application)
                await self.bot.database.set_application_transcript(
                    application_id, str(transcript_path)
                )

                application_log_channel = self.bot.get_channel(APPLICATION_LOG_CHANNEL_ID)
                if application_log_channel is None:
                    application_log_channel = await self.bot.fetch_channel(
                        APPLICATION_LOG_CHANNEL_ID
                    )
                if isinstance(
                    application_log_channel, (disnake.TextChannel, disnake.Thread)
                ):
                    await application_log_channel.send(
                        content=(
                            f"📚 **Транскрипт заявки #{application_id}** · Отклонён "
                            f"автоматически · тикет {channel.mention}"
                        ),
                        file=disnake.File(
                            str(transcript_path),
                            filename=f"application-{application_id}.json",
                        ),
                    )

                await channel.send(
                    content=(
                        "✅ Заявка отклонена, транскрипт сохранен. "
                        "Теперь тикет можно удалить."
                    ),
                    components=[
                        disnake.ui.Button(
                            label="Удалить тикет",
                            emoji="🗑️",
                            style=disnake.ButtonStyle.danger,
                            custom_id=f"application:delete:{application_id}",
                        )
                    ],
                )
            except Exception:
                logging.exception(
                    "Could not finalize automatically declined application %s",
                    application_id,
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

        family_key = custom_id.rsplit(":", 1)[-1]
        server_keys = inter.values
        if (
            family_key not in FAMILY_NAMES
            or not server_keys
            or any(server_key not in GAME_SERVERS for server_key in server_keys)
        ):
            await inter.response.send_message("Некорректный выбор.", ephemeral=True)
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
                family_key,
                ", ".join(GAME_SERVERS[server_key] for server_key in server_keys),
            )
        )

    @applications.sub_command(name="setup", description="Создать панель заявок")
    async def applications_setup(
        self,
        inter: disnake.ApplicationCommandInteraction,
        panel_channel: disnake.TextChannel = commands.Param(description="Канал с кнопкой подачи заявки"),
        ticket_category: disnake.CategoryChannel = commands.Param(description="Категория приватных тикетов"),
        interpolov_role: disnake.Role = commands.Param(description="Роль участника INTERPOLOV"),
        intolerance_role: disnake.Role = commands.Param(description="Роль участника INTOLERANCE"),
    ) -> None:
        if inter.guild_id is None:
            return
        await inter.response.defer(ephemeral=True)
        try:
            await self.bot.database.save_application_config(
                inter.guild_id,
                panel_channel.id,
                ticket_category.id,
                interpolov_role.id,
                intolerance_role.id,
            )
        except asyncpg.PostgresError:
            logging.exception("Could not save application configuration")
            await inter.edit_original_response(
                "❌ Не удалось сохранить настройки заявок в базе данных."
            )
            return
        embed = disnake.Embed(
            title="INTERPOLOV & INTOLERANCE",
            description=(
                "Выберите фаму, в которую хотите вступить, и заполните анкету. "
                "После отправки будет создан приватный тикет с рекрутёрами."
            ),
            color=disnake.Color.red(),
        )
        embed.set_footer(text="INTERPOLOV & INTOLERANCE • Общий набор")
        buttons = [
            disnake.ui.Button(
                label="Вступить в INTERPOLOV",
                emoji="🟦",
                style=disnake.ButtonStyle.primary,
                custom_id="family_application:open:interpolov",
            ),
            disnake.ui.Button(
                label="Вступить в INTOLERANCE",
                emoji="🟥",
                style=disnake.ButtonStyle.danger,
                custom_id="family_application:open:intolerance",
            ),
        ]
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
                f"INTERPOLOV: {interpolov_role.mention}\n"
                f"INTOLERANCE: {intolerance_role.mention}"
            ),
            color=disnake.Color.green(),
        )

    @commands.Cog.listener("on_button_click")
    async def application_button(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id or ""
        if custom_id == "family_application:open":
            family_key = "intolerance"
        elif custom_id.startswith("family_application:open:"):
            family_key = custom_id.rsplit(":", 1)[-1]
        else:
            family_key = None
        if family_key is not None:
            if family_key not in FAMILY_NAMES:
                return
            if inter.guild_id is None:
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
                components=server_select(family_key),
                ephemeral=True,
            )
            return
        if not custom_id.startswith("application:"):
            return

        if not isinstance(inter.author, disnake.Member):
            return
        if inter.guild_id is None:
            return
        config = await self.bot.database.get_application_config(inter.guild_id)
        if await member_access_level(self.bot, inter.author) < 2:
            await inter.response.send_message("Только рекрутёры могут обрабатывать заявки.", ephemeral=True)
            return
        try:
            _, status, raw_id = custom_id.split(":", 2)
            application_id = int(raw_id)
        except ValueError:
            return

        if status not in {"accepted", "review", "interview", "declined", "delete"}:
            return

        if status == "delete":
            application = await self.bot.database.get_application(application_id)
            if application is None or not application["transcript_path"]:
                await inter.response.send_message(
                    "Удаление недоступно: транскрипт ещё не сохранён.", ephemeral=True
                )
                return
            await inter.response.send_message("🗑️ Тикет будет удалён через секунду.", ephemeral=True)
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
            await inter.response.send_message("Заявка не найдена.", ephemeral=True)
            return
        if status != "declined":
            blacklist_entries = await self.bot.database.find_blacklist_entries(
                current_application["discord_id"],
                parse_static_ids(current_application["static_id"]),
            )
            if blacklist_entries:
                matched_ids = format_blacklist_matches(blacklist_entries)
                await inter.response.send_message(
                    "⛔ Пользователь находится в чёрном списке семьи "
                    f"(записи: **{matched_ids}**). Это действие заблокировано; "
                    "доступна только кнопка отказа.\n\n"
                    "⚠️ Информацию о чёрном списке запрещено распространять без "
                    "одобрения руководства.",
                    ephemeral=True,
                )
                return

        application = await self.bot.database.review_application(application_id, status, inter.author.id)
        if application is None:
            await inter.response.send_message("Заявка уже закрыта.", ephemeral=True)
            return
        applicant = inter.guild.get_member(application["discord_id"]) if inter.guild else None
        if applicant is None and inter.guild is not None:
            try:
                applicant = await inter.guild.fetch_member(application["discord_id"])
            except disnake.DiscordException:
                logging.warning(
                    "Could not fetch applicant %s for application %s",
                    application["discord_id"], application_id,
                )
        if status == "accepted" and applicant is not None and config is not None:
            role_column = (
                "interpolov_role_id"
                if application["family_name"] == "INTERPOLOV"
                else "intolerance_role_id"
            )
            accepted_role = inter.guild.get_role(config[role_column])
            selected_servers = {
                server.strip() for server in application["game_server"].split(",")
            }
            server_roles = [
                role
                for server, role_id in SERVER_ROLE_IDS.items()
                if server in selected_servers
                and (role := inter.guild.get_role(role_id)) is not None
            ]
            roles_to_add = ([accepted_role] if accepted_role is not None else []) + server_roles
            if roles_to_add:
                try:
                    await applicant.add_roles(
                        *roles_to_add, reason=f"Заявка #{application_id} принята"
                    )
                    applicant = await inter.guild.fetch_member(applicant.id)
                except disnake.DiscordException:
                    logging.exception("Could not add accepted family and server roles")

            if accepted_role is not None and accepted_role in applicant.roles:
                await self.bot.database.save_users([applicant], role_ids={accepted_role.id})
                profile = await self.bot.database.get_user(applicant.id)
                if profile is not None:
                    await self.bot.database.update_user_profile(
                        applicant.id,
                        game_nickname=application["game_nickname"],
                        added_by=inter.author.id,
                        reactivate=True,
                    )
                    await self.bot.database.set_user_family(
                        applicant.id, application["family_name"]
                    )
                    self.bot.dispatch("voice_eligibility_changed")
            else:
                logging.error(
                    "Accepted applicant %s was not saved: family role is missing",
                    applicant.id,
                )

        embed = inter.message.embeds[0]
        embed.color = {
            "accepted": disnake.Color.green(), "declined": disnake.Color.red(),
            "review": disnake.Color.blurple(), "interview": disnake.Color.gold(),
        }[status]
        embed.set_field_at(
            len(embed.fields) - 1,
            name="📌 Статус",
            value=f"**{STATUS_NAMES[status]}**\nРекрутёр: {inter.author.mention}",
            inline=False,
        )
        final = status in {"accepted", "declined"}
        await inter.response.edit_message(
            embed=embed,
            components=[] if final else application_buttons(application_id),
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
            try:
                transcript_path = await save_ticket_transcript(inter.channel, application)
                await self.bot.database.set_application_transcript(
                    application_id, str(transcript_path)
                )
                application_log_channel = self.bot.get_channel(APPLICATION_LOG_CHANNEL_ID)
                if application_log_channel is None:
                    application_log_channel = await self.bot.fetch_channel(
                        APPLICATION_LOG_CHANNEL_ID
                    )
                if isinstance(application_log_channel, (disnake.TextChannel, disnake.Thread)):
                    try:
                        await application_log_channel.send(
                            content=(
                                f"📚 **Транскрипт заявки #{application_id}** · "
                                f"{STATUS_NAMES[status]} · тикет {inter.channel.mention}"
                            ),
                            file=disnake.File(
                                str(transcript_path),
                                filename=f"application-{application_id}.json",
                            ),
                        )
                    except disnake.DiscordException:
                        logging.exception("Could not upload application transcript")

                delete_button = disnake.ui.Button(
                    label="Удалить тикет",
                    emoji="🗑️",
                    style=disnake.ButtonStyle.danger,
                    custom_id=f"application:delete:{application_id}",
                )
                await inter.channel.send(
                    content=(
                        "✅ Заявка рассмотрена, Транскрипт сохранен. "
                        "Теперь тикет можно удалить."
                    ),
                    components=[delete_button],
                )
            except Exception:
                logging.exception("Could not save transcript for application %s", application_id)
                await inter.channel.send(
                    "⚠️ Не удалось сохранить транскрипт. Кнопка удаления не создана; "
                    "проверьте журнал бота."
                )
        await send_admin_log(
            self.bot,
            title=f"🧾 Заявка #{application_id}: {STATUS_NAMES[status]}",
            description=f"Рекрутёр: {inter.author.mention}\nКандидат: <@{application['discord_id']}> (`{application['discord_id']}`)",
            color=embed.color,
        )
