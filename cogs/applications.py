import asyncio
import logging
import re
from typing import TYPE_CHECKING

import asyncpg
import disnake
from disnake.ext import commands

from config import APPLICATION_LOG_CHANNEL_ID, GUILD_ID, TARGET_ROLE_IDS
from log_utils import send_admin_log
from transcript import save_application_json, save_ticket_transcript

if TYPE_CHECKING:
    from bot import InterpolBot


STATUS_NAMES = {
    "pending": "Ожидает решения",
    "review": "На рассмотрении",
    "interview": "Вызван на обзвон",
    "accepted": "Принят",
    "declined": "Отклонён",
}


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
        title=f"📝 Заявление в семью #{application['id']}",
        description=f"Кандидат: {applicant.mention}",
        color=disnake.Color.gold(),
        timestamp=application["created_at"],
    )
    embed.set_thumbnail(url=applicant.display_avatar.url)
    if previous:
        history = "\n".join(
            f"`#{row['id']}` — **{STATUS_NAMES.get(row['status'], row['status'])}** · <t:{int(row['created_at'].timestamp())}:D>"
            for row in previous
        )
    else:
        history = "Заявок не найдено."
    embed.add_field(name="📚 Предыдущие заявки", value=history[:1024], inline=False)
    embed.add_field(name="🎮 Ник в игре", value=application["game_nickname"], inline=True)
    embed.add_field(name="#️⃣ Статик", value=application["static_id"], inline=True)
    embed.add_field(name="🎂 Возраст OOC", value=str(application["ooc_age"]), inline=True)
    embed.add_field(name="🎯 Цель вступления", value=application["goal"], inline=False)
    embed.add_field(name="🖼️ Ссылка на фото", value=application["screenshot_url"], inline=False)
    embed.add_field(name="👤 Пользователь", value=f"{applicant.mention}\n`{applicant}` · `{applicant.id}`", inline=False)
    embed.add_field(name="📌 Статус", value=STATUS_NAMES[application["status"]], inline=False)
    embed.set_footer(text="INTERPOL • Набор в семью")
    return embed


class ApplicationModal(disnake.ui.Modal):
    def __init__(self, cog: "ApplicationsCog", config: asyncpg.Record) -> None:
        self.cog = cog
        self.config = config
        components = [
            disnake.ui.TextInput(label="Ваш ник в игре", custom_id="game_nickname", max_length=100),
            disnake.ui.TextInput(label="Статик #", custom_id="static_id", max_length=30),
            disnake.ui.TextInput(label="Возраст OOC", custom_id="ooc_age", max_length=2),
            disnake.ui.TextInput(label="Цель вступления", custom_id="goal", style=disnake.TextInputStyle.paragraph, max_length=500),
            disnake.ui.TextInput(label="Ссылка на фото персонажа", custom_id="screenshot_url", max_length=500),
        ]
        super().__init__(title="Заявка в семью", custom_id="family_application:modal", components=components, timeout=600)

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
        if await self.cog.bot.database.has_open_application(inter.author.id):
            await inter.response.send_message("У вас уже есть открытая заявка.", ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)
        guild = inter.guild
        if guild is None:
            return
        category = guild.get_channel(self.config["category_id"])
        reviewer_role = guild.get_role(self.config["reviewer_role_id"])
        if not isinstance(category, disnake.CategoryChannel) or reviewer_role is None or guild.me is None:
            await inter.edit_original_response("Настройки тикетов устарели. Сообщите администратору.")
            return

        safe_name = re.sub(r"[^a-z0-9-]", "", inter.author.name.lower().replace("_", "-"))[:40] or str(inter.author.id)
        overwrites = {
            guild.default_role: disnake.PermissionOverwrite(view_channel=False),
            inter.author: disnake.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            reviewer_role: disnake.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: disnake.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        try:
            ticket = await category.create_text_channel(
                f"application-{safe_name}", overwrites=overwrites, reason=f"Заявка в семью от {inter.author}"
            )
        except disnake.DiscordException:
            logging.exception("Could not create application ticket")
            await inter.edit_original_response("Не удалось создать тикет. Проверьте права бота.")
            return

        application = await self.cog.bot.database.create_application(
            inter.author.id,
            str(inter.author),
            inter.text_values["game_nickname"].strip(),
            inter.text_values["static_id"].strip(),
            age,
            inter.text_values["goal"].strip(),
            screenshot_url,
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
            content=f"{reviewer_role.mention} {inter.author.mention}",
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
                        f"🧾 **Новая заявка #{application['id']}**\n"
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
    def __init__(self, bot: "InterpolBot") -> None:
        self.bot = bot

    @commands.slash_command(
        name="applications",
        description="Настройка заявок в семью",
        guild_ids=[GUILD_ID],
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    async def applications(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @applications.sub_command(name="setup", description="Создать панель заявок")
    @commands.has_permissions(administrator=True)
    async def applications_setup(
        self,
        inter: disnake.ApplicationCommandInteraction,
        panel_channel: disnake.TextChannel = commands.Param(description="Канал с кнопкой подачи заявки"),
        ticket_category: disnake.CategoryChannel = commands.Param(description="Категория приватных тикетов"),
        reviewer_role: disnake.Role = commands.Param(description="Роль рекрутёров"),
        accepted_role: disnake.Role = commands.Param(description="Роль принятого члена семьи"),
    ) -> None:
        if accepted_role.id not in TARGET_ROLE_IDS:
            await inter.response.send_message(
                "Роль принятого пользователя должна быть одной из семейных ролей `TARGET_ROLE_IDS`.",
                ephemeral=True,
            )
            return
        await self.bot.database.save_application_config(
            panel_channel.id, ticket_category.id, reviewer_role.id, accepted_role.id
        )
        embed = disnake.Embed(
            title="Вступление в семью",
            description="Нажмите кнопку ниже и заполните небольшую анкету. После отправки будет создан приватный тикет с рекрутёрами.",
            color=disnake.Color.red(),
        )
        embed.set_footer(text="INTERPOL • Набор в семью")
        button = disnake.ui.Button(
            label="Подать заявку в семью", emoji="📝", style=disnake.ButtonStyle.danger,
            custom_id="family_application:open",
        )
        message = await panel_channel.send(embed=embed, components=[button])
        await self.bot.database.set_application_panel_message(message.id)
        await inter.response.send_message(f"✅ Панель создана в {panel_channel.mention}.", ephemeral=True)
        await send_admin_log(
            self.bot,
            title="🧾 Панель заявок настроена",
            description=f"Администратор: {inter.author.mention}\nКанал: {panel_channel.mention}\nКатегория: **{ticket_category.name}**\nРекрутёры: {reviewer_role.mention}\nРоль принятого: {accepted_role.mention}",
            color=disnake.Color.green(),
        )

    @commands.Cog.listener("on_button_click")
    async def application_button(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id or ""
        if custom_id == "family_application:open":
            config = await self.bot.database.get_application_config()
            if config is None:
                await inter.response.send_message("Система заявок ещё не настроена.", ephemeral=True)
                return
            if await self.bot.database.has_open_application(inter.author.id):
                await inter.response.send_message("У вас уже есть открытая заявка.", ephemeral=True)
                return
            await inter.response.send_modal(ApplicationModal(self, config))
            return
        if not custom_id.startswith("application:"):
            return

        if not isinstance(inter.author, disnake.Member):
            return
        config = await self.bot.database.get_application_config()
        reviewer_role = inter.guild.get_role(config["reviewer_role_id"]) if config and inter.guild else None
        allowed = inter.author.guild_permissions.administrator or (
            reviewer_role is not None and reviewer_role in inter.author.roles
        )
        if not allowed:
            await inter.response.send_message("Только рекрутёры могут обрабатывать заявки.", ephemeral=True)
            return
        try:
            _, status, raw_id = custom_id.split(":", 2)
            application_id = int(raw_id)
        except ValueError:
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

        application = await self.bot.database.review_application(application_id, status, inter.author.id)
        if application is None:
            await inter.response.send_message("Заявка уже закрыта.", ephemeral=True)
            return
        applicant = inter.guild.get_member(application["discord_id"]) if inter.guild else None
        if status == "accepted" and applicant is not None and config is not None:
            accepted_role = inter.guild.get_role(config["accepted_role_id"])
            if accepted_role is not None and accepted_role.id in TARGET_ROLE_IDS:
                try:
                    await applicant.add_roles(
                        accepted_role, reason=f"Заявка #{application_id} принята"
                    )
                    applicant = await inter.guild.fetch_member(applicant.id)
                except disnake.DiscordException:
                    logging.exception("Could not add accepted family role")

            if any(role.id in TARGET_ROLE_IDS for role in applicant.roles):
                await self.bot.database.save_users([applicant])
                profile = await self.bot.database.get_user(applicant.id)
                if profile is not None:
                    await self.bot.database.update_user_profile(
                        applicant.id,
                        game_nickname=application["game_nickname"],
                        added_by=inter.author.id,
                        reactivate=True,
                    )
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
                    f"Статус вашей заявки **#{application_id}** изменён: **{STATUS_NAMES[status]}**."
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
