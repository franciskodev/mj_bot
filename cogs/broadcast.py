import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from config import BROADCAST_GUILD_IDS, BROADCAST_ROLE_IDS, GUILD_ID, TARGET_ROLE_IDS
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import IntoleranceBot


@dataclass
class BroadcastSession:
    initiator_id: int
    text: str
    members: list[disnake.Member]
    sent: int = 0
    failed: int = 0
    stop_requested: bool = False
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.pause_event.set()

    @property
    def processed(self) -> int:
        return self.sent + self.failed

    def status_text(self, status: str) -> str:
        return (
            f"**Рассылка: {status}**\n"
            f"Обработано: `{self.processed}/{len(self.members)}`\n"
            f"Отправлено: `{self.sent}` · Ошибок: `{self.failed}`"
        )


class BroadcastControlView(disnake.ui.View):
    def __init__(self, session: BroadcastSession, bot: "IntoleranceBot") -> None:
        super().__init__(timeout=None)
        self.session = session
        self.bot = bot

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id == self.session.initiator_id:
            return True

        await inter.response.send_message(
            "Управлять рассылкой может только запустивший её администратор.",
            ephemeral=True,
        )
        return False

    @disnake.ui.button(
        label="Пауза",
        style=disnake.ButtonStyle.secondary,
        emoji="⏸️",
        custom_id="broadcast:pause",
    )
    async def toggle_pause(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        if self.session.pause_event.is_set():
            self.session.pause_event.clear()
            button.label = "Продолжить"
            button.emoji = "▶️"
            status = "на паузе"
        else:
            self.session.pause_event.set()
            button.label = "Пауза"
            button.emoji = "⏸️"
            status = "выполняется"

        await inter.response.edit_message(
            content=self.session.status_text(status),
            view=self,
        )
        await send_admin_log(self.bot, title="⏯️ Управление рассылкой", description=f"Администратор: {inter.author.mention}\nСтатус: **{status}**")

    @disnake.ui.button(
        label="Остановить",
        style=disnake.ButtonStyle.danger,
        emoji="⏹️",
        custom_id="broadcast:stop",
    )
    async def stop_broadcast(
        self,
        button: disnake.ui.Button,
        inter: disnake.MessageInteraction,
    ) -> None:
        self.session.stop_requested = True
        self.session.pause_event.set()
        self.disable_all_items()
        await inter.response.edit_message(
            content=self.session.status_text("останавливается"),
            view=self,
        )
        await send_admin_log(self.bot, title="⏹️ Рассылка остановлена", description=f"Администратор: {inter.author.mention}", color=disnake.Color.red())


class BroadcastCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot
        self.current_session: BroadcastSession | None = None
        self.broadcast_task: asyncio.Task[None] | None = None

    def cog_unload(self) -> None:
        if self.broadcast_task is not None:
            self.broadcast_task.cancel()

    async def get_recipients(self, guild_id: int) -> list[disnake.Member]:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise RuntimeError(f"Guild {guild_id} is not available.")

        role_ids = set(BROADCAST_ROLE_IDS.get(guild_id, set()))
        if guild_id == GUILD_ID:
            role_ids.update(TARGET_ROLE_IDS)
            config = await self.bot.database.get_application_config(GUILD_ID)
            if config is not None:
                role_ids.update(
                    {config["interpolov_role_id"], config["intolerance_role_id"]}
                )
        members: set[disnake.Member] = set()
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None:
                logging.warning("Role %s was not found in guild %s", role_id, guild_id)
                continue

            members.update(member for member in role.members if not member.bot)

        return sorted(members, key=lambda member: member.id)

    async def run_broadcast(
        self,
        inter: disnake.ApplicationCommandInteraction,
        session: BroadcastSession,
        view: BroadcastControlView,
    ) -> None:
        try:
            for member in session.members:
                await session.pause_event.wait()
                if session.stop_requested:
                    break

                try:
                    await member.send(session.text)
                    session.sent += 1
                except disnake.DiscordException:
                    session.failed += 1
                    logging.exception("Could not send DM to member %s (%s)", member, member.id)

                if session.processed % 5 == 0:
                    await inter.edit_original_response(
                        content=session.status_text("выполняется"),
                        view=view,
                    )

                # Небольшая задержка дает время управлять рассылкой и снижает нагрузку на API.
                await asyncio.sleep(0.5)

            status = "остановлена" if session.stop_requested else "завершена"
            view.disable_all_items()
            await inter.edit_original_response(
                content=session.status_text(status),
                view=view,
            )
            await send_admin_log(self.bot, title="📣 Рассылка завершена", description=f"Итог: **{status}**\nОтправлено: **{session.sent}**\nОшибок: **{session.failed}**")
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Unexpected broadcast error")
            view.disable_all_items()
            try:
                await inter.edit_original_response(
                    content=session.status_text("завершена с ошибкой"),
                    view=view,
                )
            except disnake.DiscordException:
                logging.exception("Could not update broadcast control message")
        finally:
            if self.current_session is session:
                self.current_session = None
                self.broadcast_task = None

    @commands.slash_command(
        name="broadcast",
        description="Отправить сообщение пользователям с целевыми ролями",
        guild_ids=BROADCAST_GUILD_IDS,
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    @commands.has_permissions(administrator=True)
    async def slash_broadcast(
        self,
        inter: disnake.ApplicationCommandInteraction,
        text: str = commands.Param(
            description="Текст рассылки (до 2000 символов)",
            max_length=2000,
        ),
    ) -> None:
        if self.current_session is not None:
            await inter.response.send_message(
                "Другая рассылка уже выполняется. Остановите её или дождитесь завершения.",
                ephemeral=True,
            )
            return

        if inter.guild_id is None:
            return
        members = await self.get_recipients(inter.guild_id)
        if not members:
            await inter.response.send_message(
                "Не найдено пользователей с целевыми ролями.",
                ephemeral=True,
            )
            return

        session = BroadcastSession(inter.author.id, text, members)
        view = BroadcastControlView(session, self.bot)
        self.current_session = session

        await inter.response.send_message(
            session.status_text("запускается"),
            view=view,
            ephemeral=True,
        )
        await send_admin_log(self.bot, title="📣 Рассылка запущена", description=f"Администратор: {inter.author.mention}\nПолучателей: **{len(members)}**")
        self.broadcast_task = asyncio.create_task(
            self.run_broadcast(inter, session, view),
            name="role-broadcast",
        )
