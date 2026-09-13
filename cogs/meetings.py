import logging
from typing import TYPE_CHECKING

import asyncpg
import disnake
from disnake.ext import commands

from config import ADMIN_LOG_CHANNEL_ID, GUILD_IDS, TARGET_ROLE_IDS
from cogs.warnings import notify_warning

if TYPE_CHECKING:
    from bot import InterpolovBot


PAGE_TEXT_LIMIT = 3500


def member_line(row: asyncpg.Record, *, show_leave: bool = False) -> str:
    nickname = row["game_nickname"] or row["display_name"]
    server = row["server_name"] or "сервер не выбран"
    line = f"<@{row['discord_id']}> — **{nickname}** · {server}"
    if show_leave and row["leave_ends_at"] is not None:
        line += f" · до <t:{int(row['leave_ends_at'].timestamp())}:D>"
    return line


def build_meeting_pages(
    meeting_id: int,
    channel: disnake.VoiceChannel,
    administrator: disnake.Member,
    attendance: list[asyncpg.Record],
) -> list[disnake.Embed]:
    present = [row for row in attendance if row["present"]]
    absent_on_leave = [row for row in attendance if not row["present"] and row["on_leave"]]
    absent = [row for row in attendance if not row["present"] and not row["on_leave"]]
    color = disnake.Color.green() if not absent else disnake.Color.orange()
    summary = (
        f"Администратор: {administrator.mention}\n"
        f"Канал: {channel.mention}\n"
        f"Присутствуют: **{len(present)}**\n"
        f"Отсутствуют: **{len(absent)}**\n"
        f"В отпуске: **{len(absent_on_leave)}**"
    )
    sections = [
        (f"✅ Присутствуют — {len(present)}", [member_line(row) for row in present]),
        (f"❌ Не пришли — {len(absent)}", [member_line(row) for row in absent]),
        (
            f"🏖️ Не пришли, но в отпуске — {len(absent_on_leave)}",
            [member_line(row, show_leave=True) for row in absent_on_leave],
        ),
    ]

    page_texts: list[str] = []
    current = summary
    for heading, lines in sections:
        lines = lines or ["—"]
        heading_added = False
        for line in lines:
            prefix = f"\n\n**{heading}**\n" if not heading_added else "\n"
            if len(current) + len(prefix) + len(line) > PAGE_TEXT_LIMIT:
                page_texts.append(current)
                current = summary + f"\n\n**{heading}**\n" + line
                heading_added = True
            else:
                current += prefix + line
                heading_added = True
    page_texts.append(current)

    pages: list[disnake.Embed] = []
    total = len(page_texts)
    for index, text in enumerate(page_texts, start=1):
        embed = disnake.Embed(
            title=f"📋 Проверка собрания #{meeting_id}",
            description=text,
            color=color,
            timestamp=disnake.utils.utcnow(),
        )
        embed.set_footer(text=f"INTERPOLOV • Страница {index}/{total}")
        pages.append(embed)
    return pages


class MeetingPaginator(disnake.ui.View):
    def __init__(
        self,
        pages: list[disnake.Embed],
        bot: "InterpolovBot",
        meeting_id: int,
        absent_count: int,
        owner_id: int | None = None,
    ) -> None:
        super().__init__(timeout=3600)
        self.pages = pages
        self.bot = bot
        self.meeting_id = meeting_id
        self.owner_id = owner_id
        self.index = 0
        self.warn_absentees.disabled = absent_count == 0
        self.update_buttons()

    def update_buttons(self) -> None:
        self.previous_page.disabled = self.index == 0
        self.page_number.label = f"{self.index + 1}/{len(self.pages)}"
        self.next_page.disabled = self.index == len(self.pages) - 1

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        allowed = self.owner_id == inter.author.id if self.owner_id is not None else (
            isinstance(inter.author, disnake.Member)
            and inter.author.guild_permissions.administrator
        )
        if allowed:
            return True
        await inter.response.send_message("У вас нет доступа к управлению этим отчётом.", ephemeral=True)
        return False

    @disnake.ui.button(emoji="◀️", style=disnake.ButtonStyle.secondary)
    async def previous_page(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        self.index -= 1
        self.update_buttons()
        await inter.response.edit_message(embed=self.pages[self.index], view=self)

    @disnake.ui.button(label="1/1", style=disnake.ButtonStyle.secondary, disabled=True)
    async def page_number(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        pass

    @disnake.ui.button(emoji="▶️", style=disnake.ButtonStyle.secondary)
    async def next_page(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        self.index += 1
        self.update_buttons()
        await inter.response.edit_message(embed=self.pages[self.index], view=self)

    @disnake.ui.button(
        label="Выдать выговор неявившимся",
        emoji="⚠️",
        style=disnake.ButtonStyle.danger,
        row=1,
    )
    async def warn_absentees(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ) -> None:
        await inter.response.send_message(
            "Выдаю выговоры неявившимся…", ephemeral=True
        )
        absent_ids = await self.bot.database.get_meeting_absentees(self.meeting_id)
        issued = 0
        limits = 0
        duplicates = 0
        dm_failed = 0
        reason = f"Неявка на собрание #{self.meeting_id}"
        guild = inter.guild
        if guild is None:
            await inter.edit_original_response(content="Сервер не найден.")
            return
        for discord_id in absent_ids:
            member = guild.get_member(discord_id)
            if member is None:
                try:
                    member = await guild.fetch_member(discord_id)
                except disnake.DiscordException:
                    logging.warning("Meeting absentee %s was not found", discord_id)
                    continue
            row, count, error = await self.bot.database.issue_disciplinary_warning(
                discord_id, inter.author.id, reason, self.meeting_id
            )
            if error == "limit_reached":
                limits += 1
                continue
            if error == "already_issued":
                duplicates += 1
                continue
            if not await notify_warning(
                self.bot,
                member,
                inter.author,
                reason,
                count,
                warning_id=row["id"],
            ):
                dm_failed += 1
            issued += 1

        button.disabled = True
        try:
            await inter.message.edit(view=self)
        except disnake.DiscordException:
            logging.exception("Could not disable meeting warning button")
        await inter.edit_original_response(
            content=(
                f"⚠️ Выговоры выданы: **{issued}**. "
                f"Лимит 3/3: **{limits}**. Уже были выданы: **{duplicates}**. "
                f"ЛС не доставлено: **{dm_failed}**."
            )
        )


class MeetingsCog(commands.Cog):
    def __init__(self, bot: "InterpolovBot") -> None:
        self.bot = bot

    @commands.slash_command(
        name="meeting",
        description="Управление собраниями семьи",
        guild_ids=GUILD_IDS,
    )
    async def meeting(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @meeting.sub_command(name="check", description="Проверить явку в голосовом канале")
    async def meeting_check(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.VoiceChannel = commands.Param(
            description="Голосовой канал, в котором проходит собрание"
        ),
    ) -> None:
        await inter.response.defer(ephemeral=True)
        family_role_ids = set(TARGET_ROLE_IDS)
        family_role_ids.update(
            row["role_id"] for row in await self.bot.database.get_families(inter.guild_id)
            if row["role_id"] is not None
        )
        present_ids = [member.id for member in channel.members if not member.bot]
        family_ids = [
            member.id
            for member in channel.guild.members
            if not member.bot
            and (
                member.guild_permissions.administrator
                or any(role.id in family_role_ids for role in member.roles)
            )
        ]
        meeting_id, attendance = await self.bot.database.record_meeting_attendance(
            channel.id, inter.author.id, present_ids, family_ids
        )
        pages = build_meeting_pages(meeting_id, channel, inter.author, attendance)
        absent_count = sum(not row["present"] and not row["on_leave"] for row in attendance)
        private_view = MeetingPaginator(
            pages, self.bot, meeting_id, absent_count, inter.author.id
        )
        await inter.edit_original_response(embed=pages[0], view=private_view)

        log_channel = self.bot.get_channel(ADMIN_LOG_CHANNEL_ID)
        if log_channel is None:
            try:
                log_channel = await self.bot.fetch_channel(ADMIN_LOG_CHANNEL_ID)
            except disnake.DiscordException:
                logging.exception("Could not fetch meeting log channel")
                return
        if not isinstance(log_channel, (disnake.TextChannel, disnake.Thread)):
            logging.error("Meeting log channel %s is not a text channel", ADMIN_LOG_CHANNEL_ID)
            return
        log_view = MeetingPaginator(pages, self.bot, meeting_id, absent_count)
        await log_channel.send(embed=pages[0], view=log_view)
