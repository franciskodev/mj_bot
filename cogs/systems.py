import asyncio
import io
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

import disnake
from disnake.ext import commands

from access_control import (
    ACCESS_LEVEL_NAMES,
    PROTECTED_COMMAND_LEVELS,
    command_access_level,
    member_access_level,
)
from config import BLACKLIST_CHANNEL_ID, GUILD_IDS
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import IntoleranceBot


DEPARTMENT_NAMES = {
    "recruiters": "Рекрутеры",
    "contractors": "Контрактники",
}
DEPARTMENT_CHOICES = {
    "Рекрутеры": "recruiters",
    "Контрактники": "contractors",
}


def normalize_static_id(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().lstrip("#")).casefold()


def parse_static_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return list(dict.fromkeys(
        normalized
        for item in value.split(";")
        if (normalized := normalize_static_id(item))
    ))


def format_blacklist_matches(entries) -> str:
    shown = ", ".join(f"#{entry['id']}" for entry in entries[:20])
    if len(entries) > 20:
        shown += f" и ещё {len(entries) - 20}"
    return shown


def blacklist_embed(entry, *, removed: bool = False) -> disnake.Embed:
    discord_value = (
        f"<@{entry['discord_id']}> (`{entry['discord_id']}`)"
        if entry["discord_id"] is not None
        else "Не указан"
    )
    all_statics = "; ".join(f"#{static_id}" for static_id in entry["static_ids"])
    statics = all_statics
    if len(statics) > 3000:
        statics = statics[:2960].rsplit("; ", 1)[0]
        statics += f"\n… полный список в приложенном файле ({len(entry['static_ids'])} шт.)"
    embed = disnake.Embed(
        title=f"⛔ Чёрный список семьи #{entry['id']}",
        color=disnake.Color.dark_grey() if removed else disnake.Color.red(),
        timestamp=entry["created_at"],
    )
    embed.add_field(name="Discord", value=discord_value, inline=False)
    static_chunks = []
    while len(statics) > 1024:
        split_at = statics.rfind("; ", 0, 1024)
        if split_at <= 0:
            split_at = 1024
        static_chunks.append(statics[:split_at])
        statics = statics[split_at:].removeprefix("; ")
    static_chunks.append(statics or "Не указаны")
    for index, chunk in enumerate(static_chunks):
        embed.add_field(
            name="Статики персонажей" if index == 0 else "Статики (продолжение)",
            value=chunk,
            inline=False,
        )
    embed.add_field(name="Причина", value=entry["reason"], inline=False)
    embed.add_field(
        name="Внёс",
        value=f"<@{entry['created_by']}> (`{entry['created_by']}`)",
        inline=False,
    )
    if removed:
        embed.add_field(
            name="Статус",
            value=(
                f"✅ Снят с ЧС <t:{int(entry['removed_at'].timestamp())}:F>\n"
                f"Действие выполнил: <@{entry['removed_by']}>"
            ),
            inline=False,
        )
    else:
        embed.add_field(name="Статус", value="Запись активна", inline=False)
    embed.set_footer(text="Конфиденциально • Чёрный список семьи")
    return embed


def blacklist_remove_button(entry_id: int) -> disnake.ui.Button:
    return disnake.ui.Button(
        label="Снять с ЧС",
        emoji="🔓",
        style=disnake.ButtonStyle.danger,
        custom_id=f"blacklist:remove:{entry_id}",
    )


def blacklist_statics_file(entry) -> disnake.File | None:
    all_statics = "; ".join(f"#{static_id}" for static_id in entry["static_ids"])
    if len(all_statics) <= 3000:
        return None
    return disnake.File(
        io.BytesIO(all_statics.encode("utf-8")),
        filename=f"blacklist-{entry['id']}-statics.txt",
    )


def flatten_commands(commands_list: Iterable) -> list:
    result = []
    for command in commands_list:
        children = getattr(command, "children", None)
        if children:
            result.extend(flatten_commands(children.values()))
        else:
            result.append(command)
    return result


def command_options(command) -> list:
    if isinstance(command.body, disnake.Option):
        return list(command.body.options)
    return list(command.options)


class HelpPaginator(disnake.ui.View):
    def __init__(self, pages: list[disnake.Embed], owner_id: int) -> None:
        super().__init__(timeout=600)
        self.pages = pages
        self.owner_id = owner_id
        self.index = 0
        self.update_buttons()

    def update_buttons(self) -> None:
        self.previous_page.disabled = self.index == 0
        self.page_number.label = f"{self.index + 1}/{len(self.pages)}"
        self.next_page.disabled = self.index == len(self.pages) - 1

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id == self.owner_id:
            return True
        await inter.response.send_message(
            "Перелистывать эту справку может только вызвавший её пользователь.",
            ephemeral=True,
        )
        return False

    @disnake.ui.button(emoji="◀️", style=disnake.ButtonStyle.secondary)
    async def previous_page(self, button, inter: disnake.MessageInteraction) -> None:
        self.index -= 1
        self.update_buttons()
        await inter.response.edit_message(embed=self.pages[self.index], view=self)

    @disnake.ui.button(label="1/1", style=disnake.ButtonStyle.secondary, disabled=True)
    async def page_number(self, button, inter: disnake.MessageInteraction) -> None:
        pass

    @disnake.ui.button(emoji="▶️", style=disnake.ButtonStyle.secondary)
    async def next_page(self, button, inter: disnake.MessageInteraction) -> None:
        self.index += 1
        self.update_buttons()
        await inter.response.edit_message(embed=self.pages[self.index], view=self)


class SystemsCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot

    async def get_blacklist_channel(self):
        channel = self.bot.get_channel(BLACKLIST_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(BLACKLIST_CHANNEL_ID)
            except disnake.DiscordException:
                logging.exception("Could not fetch blacklist channel")
                return None
        if not isinstance(channel, (disnake.TextChannel, disnake.Thread)):
            return None
        return channel

    async def publish_blacklist_entry(self, entry, *, removed: bool) -> bool:
        channel = await self.get_blacklist_channel()
        if channel is None:
            logging.error(
                "Could not republish blacklist entry %s: channel is unavailable",
                entry["id"],
            )
            return False
        try:
            message = await channel.send(
                embed=blacklist_embed(entry, removed=removed),
                components=[] if removed else [blacklist_remove_button(entry["id"])],
                file=blacklist_statics_file(entry),
            )
            await self.bot.database.set_blacklist_message(entry["id"], message.id)
            return True
        except disnake.DiscordException:
            logging.exception("Could not republish blacklist entry %s", entry["id"])
            return False

    async def find_blacklist_message_deleter(
        self,
        guild: disnake.Guild,
        action: disnake.AuditLogAction,
    ) -> disnake.Member | None:
        for attempt in range(4):
            if attempt:
                await asyncio.sleep(1)
            try:
                async for audit_entry in guild.audit_logs(limit=8, action=action):
                    age = (
                        datetime.now(timezone.utc) - audit_entry.created_at
                    ).total_seconds()
                    if age > 20:
                        break
                    if action is disnake.AuditLogAction.message_delete:
                        channel = getattr(audit_entry.extra, "channel", None)
                        target_id = getattr(audit_entry.target, "id", None)
                        if (
                            getattr(channel, "id", None) != BLACKLIST_CHANNEL_ID
                            or self.bot.user is None
                            or target_id != self.bot.user.id
                        ):
                            continue
                    elif getattr(audit_entry.target, "id", None) != BLACKLIST_CHANNEL_ID:
                        continue
                    actor_id = getattr(audit_entry.user, "id", None)
                    if actor_id is None:
                        continue
                    member = guild.get_member(actor_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(actor_id)
                        except disnake.DiscordException:
                            return None
                    return member
            except disnake.DiscordException:
                logging.exception("Could not read audit log for deleted blacklist post")
                return None
        return None

    async def punish_unauthorized_blacklist_deletion(
        self,
        member: disnake.Member,
        entries,
    ) -> None:
        bot_member = member.guild.me
        removable_roles = [
            role
            for role in member.roles
            if not role.is_default()
            and not role.managed
            and bot_member is not None
            and role < bot_member.top_role
        ]
        removed_roles: list[disnake.Role] = []
        role_errors: list[str] = []
        for role in removable_roles:
            try:
                await member.remove_roles(
                    role,
                    reason="Несанкционированное удаление сообщения чёрного списка",
                )
                removed_roles.append(role)
            except disnake.DiscordException as error:
                logging.exception(
                    "Could not remove role %s after unauthorized blacklist deletion",
                    role.id,
                )
                role_errors.append(f"{role.mention}: {error}")

        removed_role_text = (
            ", ".join(role.mention for role in removed_roles)
            if removed_roles
            else "ничего не снято"
        )
        unavailable_roles = [
            role
            for role in member.roles
            if not role.is_default() and role not in removable_roles
        ]
        unavailable_text = (
            "\nНедоступны для снятия: "
            + ", ".join(role.mention for role in unavailable_roles)
            if unavailable_roles
            else ""
        )
        error_text = (
            f"\nОшибки Discord: `{'; '.join(role_errors)[:500]}`"
            if role_errors
            else ""
        )
        restored_ids = []
        for entry in entries:
            if await self.publish_blacklist_entry(entry, removed=False):
                restored_ids.append(entry["id"])
        entry_ids = [entry["id"] for entry in entries]
        await send_admin_log(
            self.bot,
            title="🚨 Несанкционированное удаление сообщения ЧС",
            description=(
                f"Нарушитель: {member.mention} (`{member.id}`)\n"
                f"Записи: **{', '.join(f'#{entry_id}' for entry_id in entry_ids)}**\n"
                f"Снятые роли: {removed_role_text}"
                f"{unavailable_text}{error_text}\n"
                "Записи остались активными.\n"
                f"Восстановленные сообщения: **{', '.join(f'#{entry_id}' for entry_id in restored_ids) or 'нет'}**."
            ),
            color=disnake.Color.red(),
        )

    async def handle_deleted_blacklist_messages(
        self,
        guild: disnake.Guild,
        message_ids: Iterable[int],
        audit_action: disnake.AuditLogAction,
    ) -> None:
        entries = []
        for message_id in message_ids:
            entry = await self.bot.database.get_active_blacklist_entry_by_message(
                message_id
            )
            if entry is not None:
                entries.append(entry)
        if not entries:
            return

        deleter = await self.find_blacklist_message_deleter(guild, audit_action)
        is_administrator = (
            deleter is not None and deleter.guild_permissions.administrator
        )
        if is_administrator:
            for original_entry in entries:
                entry = await self.bot.database.remove_blacklist_entry(
                    original_entry["id"], deleter.id
                )
                if entry is None:
                    continue
                await self.publish_blacklist_entry(entry, removed=True)
                await send_admin_log(
                    self.bot,
                    title=f"✅ Снятие с ЧС #{entry['id']} удалением сообщения",
                    description=(
                        f"Администратор: {deleter.mention} (`{deleter.id}`)\n"
                        "Исходное сообщение записи было удалено."
                    ),
                    color=disnake.Color.green(),
                )
            return

        if deleter is not None:
            await self.punish_unauthorized_blacklist_deletion(deleter, entries)
            return

        restored_ids = []
        for entry in entries:
            if await self.publish_blacklist_entry(entry, removed=False):
                restored_ids.append(entry["id"])

        await send_admin_log(
            self.bot,
            title="⚠️ Не удалось определить удалившего сообщение ЧС",
            description=(
                f"Записи: **{', '.join(f'#{entry['id']}' for entry in entries)}**\n"
                f"Восстановлены: **{', '.join(f'#{entry_id}' for entry_id in restored_ids) or 'нет'}**\n"
                "Записи оставлены активными. Проверьте право бота «Просмотр журнала аудита»."
            ),
            color=disnake.Color.orange(),
        )

    @commands.Cog.listener("on_raw_message_delete")
    async def blacklist_message_delete(
        self, payload: disnake.RawMessageDeleteEvent
    ) -> None:
        if payload.channel_id != BLACKLIST_CHANNEL_ID or payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        await self.handle_deleted_blacklist_messages(
            guild,
            [payload.message_id],
            disnake.AuditLogAction.message_delete,
        )

    @commands.Cog.listener("on_raw_bulk_message_delete")
    async def blacklist_bulk_message_delete(
        self, payload: disnake.RawBulkMessageDeleteEvent
    ) -> None:
        if payload.channel_id != BLACKLIST_CHANNEL_ID or payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        await self.handle_deleted_blacklist_messages(
            guild,
            payload.message_ids,
            disnake.AuditLogAction.message_bulk_delete,
        )

    @commands.slash_command(
        name="blacklist",
        description="Управление чёрным списком семьи",
        guild_ids=GUILD_IDS,
    )
    async def blacklist(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @blacklist.sub_command(name="add", description="Внести человека в чёрный список семьи")
    async def blacklist_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        reason: str = commands.Param(description="Причина внесения", max_length=1000),
        discord_id: str | None = commands.Param(
            default=None, description="Discord ID пользователя", max_length=30
        ),
        static_ids: str | None = commands.Param(
            default=None,
            description="Статики через точку с запятой: 123; 456; 789",
            max_length=4000,
        ),
    ) -> None:
        parsed_discord_id = None
        if discord_id:
            raw_id = discord_id.strip().removeprefix("<@").removesuffix(">").lstrip("!")
            if not raw_id.isdigit() or not 15 <= len(raw_id) <= 22:
                await inter.response.send_message(
                    "Укажите корректный Discord ID.", ephemeral=True
                )
                return
            parsed_discord_id = int(raw_id)
        parsed_statics = parse_static_ids(static_ids)
        if parsed_discord_id is None and not parsed_statics:
            await inter.response.send_message(
                "Нужно указать Discord ID, хотя бы один статик или оба значения.",
                ephemeral=True,
            )
            return
        reason = reason.strip()
        if not reason:
            await inter.response.send_message(
                "Причина не может быть пустой.", ephemeral=True
            )
            return

        channel = await self.get_blacklist_channel()
        if channel is None:
            await inter.response.send_message(
                f"Канал ЧС `{BLACKLIST_CHANNEL_ID}` недоступен боту.", ephemeral=True
            )
            return
        await inter.response.defer(ephemeral=True)
        entry = await self.bot.database.add_blacklist_entry(
            parsed_discord_id, parsed_statics, reason, inter.author.id
        )
        try:
            message = await channel.send(
                embed=blacklist_embed(entry),
                components=[blacklist_remove_button(entry["id"])],
                file=blacklist_statics_file(entry),
            )
            await self.bot.database.set_blacklist_message(entry["id"], message.id)
        except disnake.DiscordException:
            logging.exception("Could not publish blacklist entry %s", entry["id"])
            await send_admin_log(
                self.bot,
                title=f"⛔ Добавление в ЧС #{entry['id']}",
                description=(
                    f"Администратор: {inter.author.mention}\n"
                    "Запись сохранена в базе, но не опубликована в канале ЧС."
                ),
                color=disnake.Color.red(),
            )
            await inter.edit_original_response(
                "Запись сохранена в базе, но не опубликована в канале ЧС. "
                "Проверьте права бота."
            )
            return
        await inter.edit_original_response(
            f"✅ Запись ЧС `#{entry['id']}` создана в {channel.mention}."
        )
        await send_admin_log(
            self.bot,
            title=f"⛔ Добавление в ЧС #{entry['id']}",
            description=(
                f"Администратор: {inter.author.mention}\n"
                f"Discord: {f'<@{parsed_discord_id}> (`{parsed_discord_id}`)' if parsed_discord_id else 'не указан'}\n"
                f"Статики: {('; '.join(parsed_statics) or 'не указаны')[:2800]}\n"
                f"Причина: {reason}"
            ),
            color=disnake.Color.red(),
        )

    @commands.Cog.listener("on_button_click")
    async def blacklist_button(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id or ""
        if not custom_id.startswith("blacklist:"):
            return
        if (
            not isinstance(inter.author, disnake.Member)
            or await member_access_level(self.bot, inter.author) < 5
        ):
            await inter.response.send_message(
                "Только администратор может снимать записи с ЧС.", ephemeral=True
            )
            return
        try:
            action, entry_id_text = custom_id.split(":")[1:]
            entry_id = int(entry_id_text)
        except (ValueError, IndexError):
            return
        if action == "remove":
            entry = await self.bot.database.get_blacklist_entry(entry_id)
            if entry is None or not entry["active"]:
                await inter.response.send_message(
                    "Эта запись уже не активна.", ephemeral=True
                )
                return
            confirm = disnake.ui.Button(
                label="Подтвердить снятие",
                emoji="✅",
                style=disnake.ButtonStyle.danger,
                custom_id=f"blacklist:confirm:{entry_id}",
            )
            await inter.response.send_message(
                f"Подтвердите снятие записи ЧС `#{entry_id}`. Действие попадёт в журнал.",
                components=[confirm],
                ephemeral=True,
            )
            return
        if action != "confirm":
            return

        entry = await self.bot.database.remove_blacklist_entry(entry_id, inter.author.id)
        if entry is None:
            await inter.response.edit_message(
                content="Запись уже была снята другим администратором.", components=[]
            )
            return
        channel = await self.get_blacklist_channel()
        if channel is not None and entry["channel_message_id"] is not None:
            try:
                message = await channel.fetch_message(entry["channel_message_id"])
                await message.edit(embed=blacklist_embed(entry, removed=True), components=[])
            except disnake.DiscordException:
                logging.exception("Could not update removed blacklist entry %s", entry_id)
        await inter.response.edit_message(
            content=f"✅ Запись ЧС `#{entry_id}` снята.", components=[]
        )
        await send_admin_log(
            self.bot,
            title=f"✅ Снятие с ЧС #{entry_id}",
            description=(
                f"Администратор: {inter.author.mention}\n"
                f"Discord: {f'<@{entry['discord_id']}> (`{entry['discord_id']}`)' if entry['discord_id'] else 'не указан'}\n"
                f"Статики: {('; '.join(entry['static_ids']) or 'не указаны')[:3000]}"
            ),
            color=disnake.Color.green(),
        )

    @commands.slash_command(
        name="help", description="Показать справку по командам бота", guild_ids=GUILD_IDS
    )
    async def help_command(self, inter: disnake.ApplicationCommandInteraction) -> None:
        if inter.guild_id is None or not isinstance(inter.author, disnake.Member):
            return
        own_level = await member_access_level(self.bot, inter.author)
        commands_list = [
            command for command in flatten_commands(self.bot.slash_commands)
            if not command.guild_ids or inter.guild_id in command.guild_ids
        ]
        entries = []
        for command in sorted(commands_list, key=lambda item: item.qualified_name):
            level = await command_access_level(
                self.bot, inter.guild_id, command.qualified_name
            )
            options = command_options(command)
            syntax = " ".join(
                f"<{option.name}>" if option.required else f"[{option.name}]"
                for option in options
            )
            marker = "✅" if own_level >= level else "🔒"
            usage = f"/{command.qualified_name}"
            if syntax:
                usage += f" {syntax}"
            escaped_usage = usage.replace("_", "\\_")
            entries.append(
                f"{marker} **{escaped_usage}**\n"
                f"{command.description}\n"
                f"Доступ: **{level} — {ACCESS_LEVEL_NAMES[level]}**"
            )

        chunks = [entries[index:index + 6] for index in range(0, len(entries), 6)] or [[]]
        pages = []
        for index, chunk in enumerate(chunks, start=1):
            embed = disnake.Embed(
                title="📖 Помощь по командам",
                description="\n\n".join(chunk) or "Команды не найдены.",
                color=disnake.Color.blurple(),
            )
            embed.add_field(
                name="Ваш уровень",
                value=f"**{own_level} — {ACCESS_LEVEL_NAMES[own_level]}**",
                inline=False,
            )
            embed.set_footer(
                text=f"✅ доступно • 🔒 недостаточно прав • Страница {index}/{len(chunks)}"
            )
            pages.append(embed)
        await inter.response.send_message(
            embed=pages[0], view=HelpPaginator(pages, inter.author.id), ephemeral=True
        )

    @commands.slash_command(
        name="access", description="Настройка уровней доступа", guild_ids=GUILD_IDS
    )
    async def access(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @access.sub_command(name="command", description="Назначить команде уровень доступа")
    async def access_command(
        self,
        inter: disnake.ApplicationCommandInteraction,
        command_name: str = commands.Param(
            name="команда", description="Полное имя из /help, без символа /", max_length=100
        ),
        level: int = commands.Param(
            name="уровень", description="Требуемый уровень", choices=list(range(1, 7))
        ),
    ) -> None:
        if inter.guild_id is None:
            return
        command_name = command_name.strip().lstrip("/").casefold()
        valid_commands = {
            command.qualified_name.casefold()
            for command in flatten_commands(self.bot.slash_commands)
            if not command.guild_ids or inter.guild_id in command.guild_ids
        }
        if command_name not in valid_commands:
            await inter.response.send_message(
                "Команда не найдена. Скопируйте её полное имя из `/help`.", ephemeral=True
            )
            return
        current_level = await command_access_level(
            self.bot, inter.guild_id, command_name
        )
        author_level = (
            await member_access_level(self.bot, inter.author)
            if isinstance(inter.author, disnake.Member)
            else 0
        )
        if current_level == 6 and author_level < 6 and level != 6:
            await inter.response.send_message(
                "Команда защищена уровнем **6 — Разработчик**. Только разработчик "
                "может понизить её уровень доступа.",
                ephemeral=True,
            )
            return
        protected_level = PROTECTED_COMMAND_LEVELS.get(command_name)
        if protected_level is not None and level != protected_level:
            await inter.response.send_message(
                f"У этой защищённой команды зафиксирован уровень {protected_level}.",
                ephemeral=True,
            )
            return
        await self.bot.database.set_command_access_level(
            inter.guild_id, command_name, level, inter.author.id
        )
        await inter.response.send_message(
            f"✅ Для `/{command_name}` установлен уровень **{level} — {ACCESS_LEVEL_NAMES[level]}**.",
            ephemeral=True,
        )
        await send_admin_log(
            self.bot,
            title="🔐 Уровень команды изменён",
            description=(
                f"Администратор: {inter.author.mention}\nКоманда: `/{command_name}`\n"
                f"Новый уровень: **{level} — {ACCESS_LEVEL_NAMES[level]}**"
            ),
        )

    @access.sub_command(name="role_add", description="Привязать роль к уровню доступа 1–4")
    async def access_role_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        level: int = commands.Param(name="уровень", choices=[1, 2, 3, 4]),
        role: disnake.Role = commands.Param(name="роль", description="Роль Discord"),
    ) -> None:
        if inter.guild_id is None:
            return
        await self.bot.database.add_access_role(
            inter.guild_id, level, role.id, inter.author.id
        )
        await inter.response.send_message(
            f"✅ Роль {role.mention} даёт уровень **{level} — {ACCESS_LEVEL_NAMES[level]}**.",
            ephemeral=True,
        )
        await send_admin_log(
            self.bot,
            title="🔐 Роль доступа добавлена",
            description=(
                f"Администратор: {inter.author.mention}\nРоль: {role.mention}\n"
                f"Уровень: **{level} — {ACCESS_LEVEL_NAMES[level]}**"
            ),
        )

    @access.sub_command(name="role_remove", description="Убрать роль из уровня доступа")
    async def access_role_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        level: int = commands.Param(name="уровень", choices=[1, 2, 3, 4]),
        role: disnake.Role = commands.Param(name="роль", description="Роль Discord"),
    ) -> None:
        if inter.guild_id is None:
            return
        removed = await self.bot.database.remove_access_role(
            inter.guild_id, level, role.id
        )
        await inter.response.send_message(
            f"{'✅ Привязка удалена.' if removed else 'Такая привязка не найдена.'}",
            ephemeral=True,
        )
        if removed:
            await send_admin_log(
                self.bot,
                title="🔐 Роль доступа удалена",
                description=(
                    f"Администратор: {inter.author.mention}\nРоль: {role.mention}\n"
                    f"Уровень: **{level} — {ACCESS_LEVEL_NAMES[level]}**"
                ),
            )

    @access.sub_command(name="show", description="Показать настроенные роли доступа")
    async def access_show(self, inter: disnake.ApplicationCommandInteraction) -> None:
        if inter.guild_id is None:
            return
        rows = await self.bot.database.get_access_roles(inter.guild_id)
        lines = [
            f"**{row['access_level']} — {ACCESS_LEVEL_NAMES[row['access_level']]}:** "
            f"<@&{row['role_id']}>"
            for row in rows
        ]
        await inter.response.send_message(
            "\n".join(lines) or "Дополнительные роли доступа ещё не настроены.",
            ephemeral=True,
        )

    @commands.slash_command(
        name="dm_notifications",
        description="Настроить регулярные уведомления о теге и активности",
        guild_ids=GUILD_IDS,
    )
    async def dm_notifications(
        self,
        inter: disnake.ApplicationCommandInteraction,
        enabled: bool = commands.Param(
            name="включены",
            description="Отправлять регулярные уведомления людям в ЛС",
        ),
    ) -> None:
        await self.bot.database.set_dm_notifications_enabled(enabled, inter.author.id)
        status = "включены" if enabled else "отключены"
        await inter.response.send_message(
            f"{'🔔' if enabled else '🔕'} Регулярные уведомления о теге и "
            f"активности в ЛС **{status}**.",
            ephemeral=True,
        )
        await send_admin_log(
            self.bot,
            title="🔔 Настройка регулярных уведомлений",
            description=(
                f"Разработчик: {inter.author.mention}\n"
                f"Уведомления о теге и активности в ЛС: **{status}**"
            ),
            color=disnake.Color.green() if enabled else disnake.Color.orange(),
        )

    @commands.slash_command(
        name="department", description="Управление отделами семьи", guild_ids=GUILD_IDS
    )
    async def department(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    async def can_manage_department(
        self, inter: disnake.ApplicationCommandInteraction, department: str
    ) -> bool:
        if not isinstance(inter.author, disnake.Member) or inter.guild_id is None:
            return False
        level = await member_access_level(self.bot, inter.author)
        if level >= 4:
            return True
        config = await self.bot.database.get_department_config(
            inter.guild_id, department
        )
        return config is not None and any(
            role.id == config["head_role_id"] for role in inter.author.roles
        )

    async def department_denied(self, inter) -> None:
        await inter.response.send_message(
            "Этим отделом может управлять только его глава или старший состав.",
            ephemeral=True,
        )

    @department.sub_command(name="setup", description="Настроить роли отдела")
    async def department_setup(
        self,
        inter: disnake.ApplicationCommandInteraction,
        department: str = commands.Param(
            name="отдел", choices=DEPARTMENT_CHOICES
        ),
        member_role: disnake.Role = commands.Param(
            name="роль_отдела", description="Роль участников отдела"
        ),
        head_role: disnake.Role = commands.Param(
            name="роль_главы", description="Роль главы отдела"
        ),
    ) -> None:
        if inter.guild_id is None or not await self.can_manage_department(inter, department):
            await self.department_denied(inter)
            return
        await self.bot.database.set_department_config(
            inter.guild_id, department, member_role.id, head_role.id, inter.author.id
        )
        await inter.response.send_message(
            f"✅ Отдел **{DEPARTMENT_NAMES[department]}** настроен: "
            f"участники — {member_role.mention}, глава — {head_role.mention}.",
            ephemeral=True,
        )
        await send_admin_log(
            self.bot,
            title="🏢 Роли отдела настроены",
            description=(
                f"Настроил: {inter.author.mention}\nОтдел: **{DEPARTMENT_NAMES[department]}**\n"
                f"Участники: {member_role.mention}\nГлава: {head_role.mention}"
            ),
        )

    async def change_department_member(
        self, inter, department: str, member: disnake.Member, *, add: bool
    ) -> None:
        if inter.guild_id is None or not await self.can_manage_department(inter, department):
            await self.department_denied(inter)
            return
        config = await self.bot.database.get_department_config(inter.guild_id, department)
        if config is None:
            await inter.response.send_message(
                "Роли этого отдела ещё не настроены.", ephemeral=True
            )
            return
        role = inter.guild.get_role(config["member_role_id"]) if inter.guild else None
        if role is None:
            await inter.response.send_message(
                "Настроенная роль отдела больше не существует.", ephemeral=True
            )
            return
        try:
            if add:
                await member.add_roles(role, reason=f"Добавил в отдел: {inter.author}")
            else:
                await member.remove_roles(role, reason=f"Убрал из отдела: {inter.author}")
        except disnake.DiscordException:
            await inter.response.send_message(
                "Не удалось изменить роль. Проверьте иерархию ролей бота.", ephemeral=True
            )
            return
        action = "добавлен в" if add else "удалён из"
        await inter.response.send_message(
            f"✅ {member.mention} {action} отдела **{DEPARTMENT_NAMES[department]}**.",
            ephemeral=True,
        )
        await send_admin_log(
            self.bot,
            title="🏢 Состав отдела изменён",
            description=(
                f"Ответственный: {inter.author.mention}\nУчастник: {member.mention}\n"
                f"Действие: **{action} {DEPARTMENT_NAMES[department]}**"
            ),
        )

    @department.sub_command(name="add", description="Добавить участника в отдел")
    async def department_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        department: str = commands.Param(name="отдел", choices=DEPARTMENT_CHOICES),
        member: disnake.Member = commands.Param(name="участник"),
    ) -> None:
        await self.change_department_member(inter, department, member, add=True)

    @department.sub_command(name="remove", description="Убрать участника из отдела")
    async def department_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        department: str = commands.Param(name="отдел", choices=DEPARTMENT_CHOICES),
        member: disnake.Member = commands.Param(name="участник"),
    ) -> None:
        await self.change_department_member(inter, department, member, add=False)
