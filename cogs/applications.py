import asyncio
from contextlib import closing
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import asyncpg
import disnake
from disnake.ext import commands

from access_control import application_staff_role_ids, can_review_application
from config import (
    APPLICATION_COOLDOWN_DAYS,
    APPLICATION_EXTRA_ACCEPTED_ROLE_IDS,
    APPLICATION_LOG_CHANNEL_ID,
    DEVELOPER_USER_ID,
    FAMILY_MEMBER_ROLE_ID,
    FAMILY_NAME,
    GUILD_IDS,
)
from cogs.systems import format_blacklist_matches, parse_static_ids
from log_utils import application_log_channel, send_admin_log
from transcript import SavedTranscript, save_application_json, save_ticket_transcript

if TYPE_CHECKING:
    from bot import InterpolovBot


STATUS_NAMES = {
    "pending": "Ожидает решения",
    "review": "На рассмотрении",
    "interview": "Вызван на обзвон",
    "accepted": "Принят",
    "declined": "Отклонён",
}
def server_select(family_id: int, server_rows) -> disnake.ui.StringSelect:
    return disnake.ui.StringSelect(
        placeholder="Выберите игровой сервер",
        custom_id=f"family_application:server:{family_id}",
        min_values=1,
        max_values=1,
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
            "Заполните анкету для вступления в семью INTERPOLOV. "
            "После отправки будет создан приватный тикет с рекрутёрами.\n\n"
            f"Подавать заявки можно раз в {APPLICATION_COOLDOWN_DAYS} дней."
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
    embed.set_footer(text="INTERPOLOV • Общий набор")
    return embed


class ApplicationModal(disnake.ui.Modal):
    def __init__(
        self, cog: "ApplicationsCog", config: asyncpg.Record, family: asyncpg.Record,
        game_server: str,
    ) -> None:
        self.cog = cog
        self.config = config
        self.family_id = family["id"]
        self.family_name = FAMILY_NAME
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
        # Serialize submissions from multiple open forms for the same user.
        key = (inter.guild_id, inter.author.id)
        lock = self.cog.application_submission_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self.cog.ticket_access_lock.locked():
                await inter.response.send_message(
                    "Обновляются права заявок. Попробуйте отправить анкету немного позже.", ephemeral=True
                )
                return
            async with self.cog.ticket_access_lock:
                await self._submit_application(inter)

    async def _submit_application(self, inter: disnake.ModalInteraction) -> None:
        if not await self.cog.can_submit_application(inter):
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
        staff_roles = await self.cog.application_staff_roles(guild, self.game_server)
        if not isinstance(category, disnake.CategoryChannel) or not staff_roles or guild.me is None:
            await inter.edit_original_response(
                "Для выбранного сервера не настроены роли рекрутёров и High или категория тикетов. "
                "Сообщите администратору: `/applications roles`."
            )
            return

        safe_name = re.sub(r"[^a-z0-9-]", "", inter.author.name.lower().replace("_", "-"))[:40] or str(inter.author.id)
        overwrites = self.cog.ticket_overwrites(guild, inter.author, staff_roles)
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
                channel_id=APPLICATION_LOG_CHANNEL_ID,
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
        previous = [
            row for row in await self.cog.bot.database.get_previous_applications(inter.author.id, application["id"])
            if row["guild_id"] == guild.id and row["game_server"] == self.game_server
        ]
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
        archive = await application_log_channel(self.cog.bot)
        if archive is not None:
            try:
                await archive.send(
                    content=(
                        f"🧾 **Новая заявка в {self.family_name} #{application['id']}**\n"
                        f"Кандидат: {inter.author.mention} · Тикет: {ticket.mention}"
                    ),
                    embed=embed,
                    file=disnake.File(str(application_json_path), filename=f"application-{application['id']}.json") if application_json_path else None,
                )
            except disnake.DiscordException:
                logging.exception("Could not send application log")
        await inter.edit_original_response(f"✅ Заявка создана: {ticket.mention}")


class ApplicationsCog(commands.Cog):
    def __init__(self, bot: "InterpolovBot") -> None:
        self.bot = bot
        self.ticket_access_lock = asyncio.Lock()
        self.application_submission_locks: WeakValueDictionary[
            tuple[int | None, int], asyncio.Lock
        ] = WeakValueDictionary()

    async def can_submit_application(
        self, inter: disnake.MessageInteraction | disnake.ModalInteraction
    ) -> bool:
        if inter.guild_id not in GUILD_IDS:
            return False
        try:
            blocked = (
                inter.author.id != DEVELOPER_USER_ID
                and await self.bot.database.is_application_blocked(
                    inter.guild_id, inter.author.id
                )
            )
            cooldown_until = (
                await self.bot.database.get_application_cooldown_until(
                    inter.guild_id, inter.author.id
                ) if not blocked else None
            )
        except (asyncpg.PostgresError, OSError, RuntimeError):
            logging.exception("Could not check application eligibility")
            await inter.response.send_message(
                "Не удалось проверить доступ к заявкам. Попробуйте позже.",
                ephemeral=True,
            )
            return False
        if blocked:
            await inter.response.send_message(
                "Вам заблокирована подача заявок. Обратитесь к администрации.",
                ephemeral=True,
            )
            return False
        if cooldown_until is not None and cooldown_until > disnake.utils.utcnow():
            timestamp = int(cooldown_until.timestamp())
            await inter.response.send_message(
                f"Подавать заявки можно раз в {APPLICATION_COOLDOWN_DAYS} дней. "
                f"Следующая подача доступна <t:{timestamp}:F> (<t:{timestamp}:R>).",
                ephemeral=True,
            )
            return False
        return True

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await application_log_channel(self.bot)
        for guild_id in GUILD_IDS:
            failures = await self.sync_ticket_access(guild_id)
            if failures:
                logging.error("Could not fully migrate %s application tickets in guild %s", failures, guild_id)
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
        self, guild: disnake.Guild, game_server: str
    ) -> list[disnake.Role]:
        role_ids = await application_staff_role_ids(self.bot, guild.id, game_server)
        roles = [guild.get_role(role_id) for role_id in sorted(role_ids)]
        if len(roles) != 2 or any(role is None or role.is_default() for role in roles):
            return []
        return roles

    @staticmethod
    def ticket_overwrites(guild, applicant, staff_roles) -> dict:
        overwrites = {
            guild.default_role: disnake.PermissionOverwrite(view_channel=False),
            guild.me: disnake.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_channels=True, attach_files=True, embed_links=True,
            ),
        }
        if applicant is not None:
            overwrites[applicant] = disnake.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
            )
        for role in staff_roles:
            overwrites[role] = disnake.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
            )
        return overwrites

    async def sync_ticket_access(self, guild_id: int) -> int:
        async with self.ticket_access_lock:
            return await self._sync_ticket_access(guild_id)

    async def _sync_ticket_access(self, guild_id: int) -> int:
        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.me is None:
            return 1
        failures = 0
        for application in await self.bot.database.get_ticket_applications(guild_id):
            try:
                channel = guild.get_channel(application["ticket_channel_id"])
                if channel is None:
                    try:
                        channel = await guild.fetch_channel(application["ticket_channel_id"])
                    except disnake.NotFound:
                        continue
                if not isinstance(channel, disnake.TextChannel) or channel.guild.id != guild_id:
                    continue
                applicant = guild.get_member(application["discord_id"])
                # Revoke the old grants before cleaning historical cards. If an
                # API call fails, do not expose another server's old history.
                private_overwrites = self.ticket_overwrites(guild, applicant, [])
                if channel.overwrites != private_overwrites:
                    await channel.edit(overwrites=private_overwrites, reason="Обновление приватности заявки")
                if applicant is None:
                    try:
                        applicant = await guild.fetch_member(application["discord_id"])
                    except disnake.NotFound:
                        pass
                if application["ticket_message_id"] is not None:
                    message = await channel.fetch_message(application["ticket_message_id"])
                    if not await self._sync_application_message(message, application):
                        failures += 1
                        continue
                roles = await self.application_staff_roles(guild, application["game_server"])
                overwrites = self.ticket_overwrites(guild, applicant, roles)
                if channel.overwrites != overwrites:
                    await channel.edit(overwrites=overwrites, reason="Доступ к заявке по серверным ролям")
            except disnake.DiscordException:
                failures += 1
                logging.exception("Could not synchronize access for application %s", application["id"])
        return failures

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
            if (
                application["transcript_path"]
                and Path(application["transcript_path"]).suffix == ".html"
                and Path(application["transcript_path"]).is_file()
                and application["transcript_log_message_id"]
            ):
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
    ) -> SavedTranscript | None:
        application_id = application["id"]
        for attempt in range(1, 4):
            try:
                transcript = await save_ticket_transcript(channel, application)
                await self.bot.database.set_application_transcript(
                    application_id, str(transcript.html_path)
                )
                return transcript
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
        embed.title = f"📝 Заявление в {FAMILY_NAME} #{application['id']}"
        embed.set_footer(text=f"{FAMILY_NAME} • Набор в семью")
        previous = [
            row for row in await self.bot.database.get_previous_applications(
                application["discord_id"], application["id"]
            )
            if row["guild_id"] == application["guild_id"]
            and row["game_server"] == application["game_server"]
        ]
        history = "\n".join(
            f"`#{row['id']}` · **{FAMILY_NAME}** — "
            f"**{STATUS_NAMES.get(row['status'], row['status'])}** · "
            f"<t:{int(row['created_at'].timestamp())}:D>" for row in previous
        ) or "Заявок не найдено."
        for index, field in enumerate(embed.fields):
            if field.name == "🤝 Выбранная фама":
                embed.set_field_at(index, name=field.name, value=f"**{FAMILY_NAME}**", inline=False)
            elif field.name == "📚 Предыдущие заявки":
                embed.set_field_at(index, name=field.name, value=history[:1024], inline=False)

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
        application: asyncpg.Record,
        status_text: str,
        transcript: SavedTranscript,
    ) -> bool:
        application_id = application["id"]
        archive = await application_log_channel(self.bot)
        if archive is None:
            return False
        embed = disnake.Embed(
            title=f"📚 Заявка #{application_id} — {status_text}",
            description="Скачайте HTML-файл и откройте его в браузере, чтобы прочитать переписку.",
            color=disnake.Color.green() if application["status"] == "accepted" else disnake.Color.red(),
            timestamp=disnake.utils.utcnow(),
        )
        embed.add_field(name="Заявитель", value=f"<@{application['discord_id']}>\n`{application['discord_id']}`")
        embed.add_field(name="Игровой сервер", value=application["game_server"][:1024])
        embed.add_field(name="Результат", value=status_text)
        embed.add_field(name="Рассмотрел", value=f"<@{application['reviewer_id']}>" if application["reviewer_id"] else "Не указан")
        embed.add_field(name="Сообщений", value=str(transcript.message_count))
        embed.add_field(name="Вложений", value=str(transcript.attachment_count))
        if transcript.linked_attachment_count:
            embed.add_field(
                name="Вложения по ссылкам",
                value=f"{transcript.linked_attachment_count} — крупные или недоступные для встраивания. Ссылки Discord могут истечь.",
                inline=False,
            )
        embed.set_footer(text="INTERPOLOV • HTML-архив • Время сообщений в UTC")
        try:
            message = None
            if application.get("transcript_log_message_id"):
                try:
                    message = await archive.fetch_message(application["transcript_log_message_id"])
                except disnake.NotFound:
                    pass
            with closing(disnake.File(str(transcript.html_path), filename=f"application-{application_id}.html")) as file:
                if message is None:
                    message = await archive.send(embed=embed, file=file, allowed_mentions=disnake.AllowedMentions.none())
                else:
                    await message.edit(content=None, embed=embed, file=file, attachments=[],
                                       allowed_mentions=disnake.AllowedMentions.none())
            await self.bot.database.set_application_transcript_log_message(application_id, message.id)
            return True
        except (disnake.DiscordException, asyncpg.PostgresError, OSError, RuntimeError):
            logging.exception(
                "Could not upload transcript for application %s", application_id
            )
            return False

    async def _finalize_ticket(
        self,
        channel: disnake.TextChannel,
        application: asyncpg.Record,
        status_text: str,
        prompt_text: str,
    ) -> bool:
        application_id = application["id"]
        transcript = await self._save_ticket_transcript_with_retry(
            channel, application
        )
        if transcript is None:
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
            return False

        if not await self._upload_ticket_transcript(channel, application, status_text, transcript):
            try:
                await channel.send(
                    "⚠️ HTML-транскрипт сохранён, но не удалось отправить его в журнал. "
                    "Тикет пока нельзя удалить; проверьте права бота, размер файла и повторите действие."
                )
            except disnake.DiscordException:
                logging.exception("Could not report transcript upload failure for application %s", application_id)
            return False
        await self._ensure_ticket_delete_prompt(
            channel, application_id, prompt_text
        )
        return True

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
            channel_id=APPLICATION_LOG_CHANNEL_ID,
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

    @applications.sub_command(name="roles", description="Настроить рекрутёров и High игрового сервера (уровень 5)")
    async def applications_roles(
        self,
        inter: disnake.ApplicationCommandInteraction,
        server: str = commands.Param(description="Выберите игровой сервер из списка заявок", max_length=80),
        recruiter_role: disnake.Role = commands.Param(description="Отдельная роль рекрутёров этого сервера"),
        high_role: disnake.Role = commands.Param(description="Отдельная роль High этого сервера"),
    ) -> None:
        if inter.guild_id not in GUILD_IDS or inter.guild is None:
            return
        servers = await self.bot.database.get_game_servers(inter.guild_id)
        selected = next((row for row in servers if row["name"].casefold() == server.strip().casefold()), None)
        if selected is None or selected["role_id"] is None or inter.guild.get_role(selected["role_id"]) is None:
            await inter.response.send_message(
                "Выберите существующий сервер из списка. Настроить сервер и его роль можно через `/settings server_add`.",
                ephemeral=True,
            )
            return
        member_role_ids = {row["role_id"] for row in servers} | {
            row["role_id"] for row in await self.bot.database.get_families(inter.guild_id)
        } | {FAMILY_MEMBER_ROLE_ID, *APPLICATION_EXTRA_ACCEPTED_ROLE_IDS}
        roles = (recruiter_role, high_role)
        if recruiter_role == high_role or any(
            role.is_default() or role.managed or role.permissions.administrator
            or role.id in member_role_ids for role in roles
        ):
            await inter.response.send_message(
                "Выберите две отдельные роли сотрудников этого сервера. "
                "Общие роли участников, ботов и администраторов не подходят; "
                "администраторы Discord уже видят все заявки.", ephemeral=True,
            )
            return
        await inter.response.defer(ephemeral=True)
        async with self.ticket_access_lock:
            try:
                await self.bot.database.set_application_staff_roles(
                    inter.guild_id, selected["id"], recruiter_role.id, high_role.id,
                )
            except asyncpg.UniqueViolationError:
                await inter.edit_original_response(
                    "Одна из ролей уже используется для заявок другого сервера. Выберите отдельную роль."
                )
                return
            except (asyncpg.PostgresError, ValueError, RuntimeError):
                logging.exception("Could not configure application roles")
                await inter.edit_original_response("Не удалось сохранить роли. Проверьте настройки сервера и повторите.")
                return
            failures = await self._sync_ticket_access(inter.guild_id)
        archive = await application_log_channel(self.bot)
        result = (
            f"✅ **{selected['name']}**: рекрутёры — {recruiter_role.mention} (уровень 2), "
            f"High — {high_role.mention} (уровень 4). Права существующих тикетов обновлены."
        )
        if failures:
            result = result.replace("Права существующих тикетов обновлены.",
                f"Не удалось обновить {failures} тикет(ов). Проверьте права бота и повторите команду.")
        if archive is None:
            result += "\n⚠️ Не удалось закрыть общий архив заявок. Проверьте права бота на этот канал."
        await inter.edit_original_response(result)

    @applications_roles.autocomplete("server")
    async def applications_roles_server_autocomplete(
        self, inter: disnake.ApplicationCommandInteraction, query: str
    ) -> list[str]:
        if inter.guild_id not in GUILD_IDS or inter.guild is None:
            return []
        try:
            servers = await self.bot.database.get_game_servers(inter.guild_id)
        except (asyncpg.PostgresError, OSError, RuntimeError):
            logging.exception("Could not load application servers for autocomplete")
            return []
        query = query.strip().casefold()
        return [
            row["name"] for row in servers
            if row["role_id"] is not None
            and inter.guild.get_role(row["role_id"]) is not None
            and query in row["name"].casefold()
        ][:25]

    @applications.sub_command(name="roles_show", description="Показать доступ к заявкам по серверам (уровень 5)")
    async def applications_roles_show(self, inter: disnake.ApplicationCommandInteraction) -> None:
        if inter.guild_id not in GUILD_IDS:
            return
        rows = await self.bot.database.get_application_staff_roles(inter.guild_id)
        servers = await self.bot.database.get_game_servers(inter.guild_id)
        lines = []
        for server in servers:
            labels = [
                f"{'Рекрутёры' if row['access_level'] == 2 else 'High'}: <@&{row['role_id']}>"
                for row in rows if row["server_id"] == server["id"]
            ]
            lines.append(f"**{server['name']}** — " + (", ".join(labels) or "роли не настроены"))
        await inter.response.send_message("\n".join(lines) or "Игровые серверы не настроены.", ephemeral=True)

    @applications.sub_command(name="block", description="Заблокировать подачу заявок пользователю (уровень 5)")
    async def applications_block(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(description="Пользователь, которому запретить подачу заявок"),
        reason: str = commands.Param(description="Причина блокировки", max_length=1000),
    ) -> None:
        if inter.guild_id not in GUILD_IDS:
            return
        if user.id == DEVELOPER_USER_ID:
            await inter.response.send_message(
                "Нельзя заблокировать подачу заявок разработчику (уровень 6).",
                ephemeral=True,
            )
            return
        reason = reason.strip()
        if not reason:
            await inter.response.send_message("Укажите причину блокировки.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        try:
            await self.bot.database.block_applications(
                inter.guild_id, user.id, reason, inter.author.id
            )
        except (asyncpg.PostgresError, OSError, RuntimeError):
            logging.exception("Could not block applications")
            await inter.edit_original_response("Не удалось сохранить блокировку. Попробуйте позже.")
            return
        await inter.edit_original_response(f"✅ Подача заявок для {user.mention} заблокирована.")
        await send_admin_log(
            self.bot,
            channel_id=APPLICATION_LOG_CHANNEL_ID,
            title="🛑 Подача заявок заблокирована",
            description=(
                f"Пользователь: {user.mention} (`{user.id}`)\n"
                f"Администратор: {inter.author.mention}\nПричина: {reason}"
            ),
            color=disnake.Color.red(),
        )

    @applications.sub_command(name="unblock", description="Разрешить пользователю снова подавать заявки (уровень 5)")
    async def applications_unblock(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(description="Пользователь, с которого снять блокировку"),
    ) -> None:
        if inter.guild_id not in GUILD_IDS:
            return
        await inter.response.defer(ephemeral=True)
        try:
            removed = await self.bot.database.unblock_applications(inter.guild_id, user.id)
        except (asyncpg.PostgresError, OSError, RuntimeError):
            logging.exception("Could not unblock applications")
            await inter.edit_original_response("Не удалось снять блокировку. Попробуйте позже.")
            return
        if not removed:
            await inter.edit_original_response("У пользователя нет блокировки подачи заявок.")
            return
        await inter.edit_original_response(f"✅ {user.mention} снова может подавать заявки.")
        await send_admin_log(
            self.bot,
            channel_id=APPLICATION_LOG_CHANNEL_ID,
            title="✅ Блокировка подачи заявок снята",
            description=f"Пользователь: {user.mention} (`{user.id}`)\nАдминистратор: {inter.author.mention}",
            color=disnake.Color.green(),
        )

    @applications.sub_command(
        name="reset_cooldown",
        description="Сбросить кулдаун подачи заявки пользователю (уровень 5)",
    )
    async def applications_reset_cooldown(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(description="Пользователь, которому сбросить кулдаун заявки"),
    ) -> None:
        if inter.guild_id not in GUILD_IDS:
            return
        await inter.response.defer(ephemeral=True)
        lock = self.application_submission_locks.setdefault(
            (inter.guild_id, user.id), asyncio.Lock()
        )
        async with lock:
            try:
                await self.bot.database.reset_application_cooldown(inter.guild_id, user.id)
            except (asyncpg.PostgresError, OSError, RuntimeError):
                logging.exception("Could not reset application cooldown")
                await inter.edit_original_response("Не удалось сбросить кулдаун. Попробуйте позже.")
                return
        await inter.edit_original_response(
            f"✅ Кулдаун подачи заявки для {user.mention} сброшен."
        )
        await send_admin_log(
            self.bot,
            channel_id=APPLICATION_LOG_CHANNEL_ID,
            title="⏱️ Кулдаун подачи заявки сброшен",
            description=f"Пользователь: {user.mention} (`{user.id}`)\nАдминистратор: {inter.author.mention}",
            color=disnake.Color.green(),
        )

    @commands.Cog.listener("on_dropdown")
    async def application_server_select(
        self, inter: disnake.MessageInteraction
    ) -> None:
        custom_id = inter.component.custom_id or ""
        if not custom_id.startswith("family_application:server:"):
            return
        if not await self.can_submit_application(inter):
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
        if family is None or family["name"] != FAMILY_NAME or len(chosen_servers) != 1 or len(server_ids) != 1:
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
        if panel_channel.id == APPLICATION_LOG_CHANNEL_ID:
            await inter.response.send_message(
                "Выберите другой канал: общий архив заявок доступен только администраторам.", ephemeral=True
            )
            return
        family_role = inter.guild.get_role(FAMILY_MEMBER_ROLE_ID)
        if family_role is None:
            await inter.response.send_message(
                "Роль семьи INTERPOLOV не найдена на сервере.", ephemeral=True
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
            channel_id=APPLICATION_LOG_CHANNEL_ID,
            title="🧾 Панель заявок настроена",
            description=(
                f"Администратор: {inter.author.mention}\n"
                f"Канал: {panel_channel.mention}\n"
                f"Категория: **{ticket_category.name}**\n"
                "Доступ к тикетам: **рекрутёры и High выбранного сервера**\n"
                f"Семья: {family_role.mention}"
            ),
            color=disnake.Color.green(),
        )

    @commands.Cog.listener("on_button_click")
    async def application_button(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id or ""
        if custom_id == "family_application:open" or custom_id.startswith("family_application:open:"):
            if not await self.can_submit_application(inter):
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
                    "Семья INTERPOLOV или игровые серверы ещё не настроены.", ephemeral=True
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
                "Выберите сервер, на который подаёте заявку:",
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
        current_application = await self.bot.database.get_application(application_id)
        if (
            current_application is None
            or current_application["guild_id"] != inter.guild_id
            or current_application["ticket_channel_id"] != inter.channel_id
            or not await can_review_application(self.bot, inter.author, current_application)
        ):
            await inter.followup.send(
                "Обрабатывать заявку могут только рекрутёры и High её сервера.", ephemeral=True
            )
            return

        if status == "delete":
            application = await self.bot.database.get_application(application_id)
            if application is None or application["status"] not in {"accepted", "declined"}:
                await inter.followup.send(
                    "Удаление недоступно: заявка ещё не рассмотрена.", ephemeral=True
                )
                return
            if not isinstance(inter.channel, disnake.TextChannel):
                return
            # Capture messages sent after the decision before removing the ticket.
            if not await self._finalize_ticket(
                inter.channel, application, STATUS_NAMES[application["status"]],
                "✅ HTML-транскрипт отправлен в журнал. Теперь тикет можно удалить.",
            ):
                await inter.followup.send("Удаление отменено: архивирование не завершено.", ephemeral=True)
                return
            await inter.followup.send(
                "🗑️ Тикет будет удалён через секунду.", ephemeral=True
            )
            await send_admin_log(
                self.bot,
                channel_id=APPLICATION_LOG_CHANNEL_ID,
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
                (not current_application["transcript_path"] or not current_application.get("transcript_log_message_id"))
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
            channel_id=APPLICATION_LOG_CHANNEL_ID,
            title=f"🧾 Заявка #{application_id}: {STATUS_NAMES[status]}",
            description=f"Рекрутёр: {inter.author.mention}\nКандидат: <@{application['discord_id']}> (`{application['discord_id']}`)",
            color={
                "accepted": disnake.Color.green(),
                "declined": disnake.Color.red(),
                "review": disnake.Color.blurple(),
                "interview": disnake.Color.gold(),
            }[status],
        )
