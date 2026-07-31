import logging
from typing import TYPE_CHECKING

import asyncpg
import disnake
from disnake.ext import commands

from config import ADMIN_LOG_CHANNEL_ID, GUILD_ID, TARGET_ROLE_IDS

if TYPE_CHECKING:
    from bot import InterpolBot


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
        embed.set_footer(text=f"INTERPOL • Страница {index}/{total}")
        pages.append(embed)
    return pages


class MeetingPaginator(disnake.ui.View):
    def __init__(self, pages: list[disnake.Embed], owner_id: int | None = None) -> None:
        super().__init__(timeout=3600)
        self.pages = pages
        self.owner_id = owner_id
        self.index = 0
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


class MeetingsCog(commands.Cog):
    def __init__(self, bot: "InterpolBot") -> None:
        self.bot = bot

    @commands.slash_command(
        name="meeting",
        description="Управление собраниями семьи",
        guild_ids=[GUILD_ID],
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    async def meeting(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @meeting.sub_command(name="check", description="Проверить явку в голосовом канале")
    @commands.has_permissions(administrator=True)
    async def meeting_check(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.VoiceChannel = commands.Param(
            description="Голосовой канал, в котором проходит собрание"
        ),
    ) -> None:
        await inter.response.defer(ephemeral=True)
        present_ids = [member.id for member in channel.members if not member.bot]
        family_ids = [
            member.id
            for member in channel.guild.members
            if not member.bot
            and any(role.id in TARGET_ROLE_IDS for role in member.roles)
        ]
        meeting_id, attendance = await self.bot.database.record_meeting_attendance(
            channel.id, inter.author.id, present_ids, family_ids
        )
        pages = build_meeting_pages(meeting_id, channel, inter.author, attendance)
        private_view = MeetingPaginator(pages, inter.author.id) if len(pages) > 1 else None
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
        log_view = MeetingPaginator(pages) if len(pages) > 1 else None
        await log_channel.send(embed=pages[0], view=log_view)
