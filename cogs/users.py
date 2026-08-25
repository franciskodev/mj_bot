import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import asyncpg
import disnake
from disnake.ext import commands, tasks

from config import FAMILY_NAME, GUILD_ID, GUILD_IDS, TARGET_ROLE_IDS
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import IntoleranceBot


STATUS_COLORS = {
    "Активен": disnake.Color.green(),
    "В отпуске": disnake.Color.gold(),
    "Неактивен": disnake.Color.red(),
}


def selected_servers(server_name: str | None, valid_names: set[str]) -> set[str]:
    return {
        server.strip()
        for server in (server_name or "").split(",")
        if server.strip() in valid_names
    }


def servers_from_roles(member: disnake.Member, server_rows) -> list[str]:
    member_role_ids = {role.id for role in member.roles}
    return [
        row["name"]
        for row in server_rows
        if row["role_id"] is not None and row["role_id"] in member_role_ids
    ]


async def sync_server_roles(
    member: disnake.Member, servers: set[str], server_rows, *, reason: str
) -> bool:
    selected_role_ids = {
        row["role_id"] for row in server_rows
        if row["name"] in servers and row["role_id"] is not None
    }
    server_roles = {
        role_id: member.guild.get_role(role_id)
        for row in server_rows
        if (role_id := row["role_id"]) is not None
    }
    if any(role is None for role in server_roles.values()):
        return False
    roles_to_add = [
        role
        for role_id, role in server_roles.items()
        if role_id in selected_role_ids and role not in member.roles
    ]
    roles_to_remove = [
        role
        for role_id, role in server_roles.items()
        if role_id not in selected_role_ids and role in member.roles
    ]
    try:
        if roles_to_add:
            await member.add_roles(*roles_to_add, reason=reason)
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason=reason)
    except disnake.DiscordException:
        logging.exception("Could not synchronize server roles for member %s", member.id)
        return False
    return True


def format_date(value: datetime | None) -> str:
    if value is None:
        return "—"
    return f"<t:{int(value.timestamp())}:F>"


def profile_embed(
    user: disnake.User | disnake.Member,
    profile: asyncpg.Record,
    weekly_voice_seconds: int | None = None,
    total_voice_seconds: int | None = None,
    warning_count: int | None = None,
) -> disnake.Embed:
    status = profile["profile_status"]
    embed = disnake.Embed(
        title="Профиль участника",
        description=f"### {user.mention}\n{profile['bio'] or '*Описание пока не заполнено.*'}",
        color=STATUS_COLORS.get(status, disnake.Color.blurple()),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="🎮 Игровой ник", value=profile["game_nickname"] or "—", inline=True)
    embed.add_field(name="🌐 Сервер", value=profile["server_name"] or "—", inline=True)
    embed.add_field(name="🤝 Семья", value=profile["family_name"] or "—", inline=True)
    embed.add_field(name="📌 Статус", value=status, inline=True)
    if warning_count is not None:
        embed.add_field(
            name="⚠️ Выговоры", value=f"**{warning_count}/3**", inline=True
        )
    if not profile["is_active"]:
        embed.add_field(
            name="⛔ Причина деактивации",
            value=profile["inactive_reason"] or "Причина не указана",
            inline=False,
        )
    if weekly_voice_seconds is not None:
        hours, remainder = divmod(weekly_voice_seconds, 3600)
        minutes = remainder // 60
        embed.add_field(name="🎙️ Войс за 7 дней", value=f"`{hours} ч. {minutes} мин.`", inline=True)
    if total_voice_seconds is not None:
        hours, remainder = divmod(total_voice_seconds, 3600)
        minutes = remainder // 60
        embed.add_field(name="🎙️ Войс за всё время", value=f"`{hours} ч. {minutes} мин.`", inline=True)
    embed.add_field(name="🆔 Discord ID", value=f"`{user.id}`", inline=True)
    embed.add_field(name="👤 Discord", value=f"`{user}`", inline=True)
    embed.add_field(
        name="📅 На сервере с",
        value=format_date(profile["guild_joined_at"]),
        inline=False,
    )
    embed.set_footer(text="INTERPOLOV & INTOLERANCE • Профиль участника")
    return embed


class ServerSelect(disnake.ui.StringSelect):
    def __init__(
        self,
        bot: "IntoleranceBot",
        owner: disnake.Member,
        current_servers: str | None,
        server_rows,
    ) -> None:
        self.bot = bot
        self.profile_owner = owner
        self.owner_id = owner.id
        selected = {
            server.strip()
            for server in (current_servers or "").split(",")
            if server.strip()
        }
        self.server_rows = server_rows
        options = [
            disnake.SelectOption(
                label=row["name"], value=row["name"], default=row["name"] in selected
            )
            for row in server_rows[:25]
        ]
        super().__init__(
            placeholder="Выберите игровой сервер",
            min_values=1,
            max_values=len(options),
            options=options,
            custom_id=f"myinfo:servers:{owner.id}",
        )

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if inter.author.id != self.owner_id:
            await inter.response.send_message(
                "Изменять этот профиль может только его владелец.", ephemeral=True
            )
            return

        server_name = ", ".join(self.values)
        profile = await self.bot.database.update_user_profile(
            self.owner_id, server_name=server_name
        )
        role_sync_succeeded = await sync_server_roles(
            self.profile_owner,
            set(self.values),
            self.server_rows,
            reason="Выбор сервера в /myinfo",
        )
        weekly_voice_seconds, total_voice_seconds, warning_count = await asyncio.gather(
            self.bot.database.get_weekly_voice_seconds(self.owner_id),
            self.bot.database.get_total_voice_seconds(self.owner_id),
            self.bot.database.get_active_disciplinary_warning_count(self.owner_id),
        )
        for option in self.options:
            option.default = option.value in self.values
        await inter.response.edit_message(
            content=(
                f"✅ Сервер обновлён: **{server_name}**"
                if role_sync_succeeded
                else f"⚠️ Сервер обновлён: **{server_name}**, но бот не смог обновить роли."
            ),
            embed=profile_embed(
                self.profile_owner,
                profile,
                weekly_voice_seconds,
                total_voice_seconds,
                warning_count,
            ) if profile else None,
            view=self.view,
        )


class UserServerView(disnake.ui.View):
    def __init__(
        self,
        bot: "IntoleranceBot",
        owner: disnake.Member,
        current_servers: str | None,
        server_rows,
    ) -> None:
        super().__init__(timeout=300)
        self.add_item(ServerSelect(bot, owner, current_servers, server_rows))



class UsersCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot
        self.sync_profile_servers_from_roles.start()

    def cog_unload(self) -> None:
        self.sync_profile_servers_from_roles.cancel()

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: disnake.Member,
        after: disnake.Member,
    ) -> None:
        if after.guild.id != GUILD_ID or after.bot:
            return

        server_rows = await self.bot.database.get_game_servers(after.guild.id)
        server_role_ids = {row["role_id"] for row in server_rows if row["role_id"] is not None}
        before_server_roles = {role.id for role in before.roles} & server_role_ids
        after_server_roles = {role.id for role in after.roles} & server_role_ids
        if before_server_roles == after_server_roles:
            return
        if await self.bot.database.get_user(after.id) is None:
            return

        servers = servers_from_roles(after, server_rows)
        await self.bot.database.update_user_profile(
            after.id,
            server_name=", ".join(servers) if servers else "-",
        )

    @tasks.loop(hours=6)
    async def sync_profile_servers_from_roles(self) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return

        profiles = await self.bot.database.get_active_users()
        server_rows = await self.bot.database.get_game_servers(guild.id)
        valid_server_names = {row["name"] for row in server_rows}
        for profile in profiles:
            member = guild.get_member(profile["discord_id"])
            if (
                member is None
                or member.bot
                or member.guild_permissions.administrator
            ):
                continue

            servers = servers_from_roles(member, server_rows)
            server_name = ", ".join(servers)
            if selected_servers(profile["server_name"], valid_server_names) != set(servers):
                await self.bot.database.update_user_profile(
                    member.id,
                    server_name=server_name if servers else "-",
                )

            if (
                servers
                or profile["profile_status"] != "Активен"
            ):
                continue

            if not await self.bot.database.is_profile_server_reminder_due(member.id):
                continue

            embed = disnake.Embed(
                title="🎮 Выберите игровой сервер",
                description=(
                    "В вашем профиле не выбран сервер, на котором вы играете. "
                    "Пожалуйста, обновите свой профиль.\n\n"
                    "**Как выбрать сервер:**\n"
                    "1. Откройте наш Discord-сервер.\n"
                    "2. Введите команду `/myinfo`.\n"
                    "3. Нажмите на список **«Выберите игровой сервер»**.\n"
                    "4. Выберите нужный игровой сервер или несколько серверов.\n"
                    "5. Нажмите **«Отправить»** для сохранения.\n\n"
                    "После сохранения бот автоматически выдаст нужную роль."
                ),
                color=disnake.Color.gold(),
                timestamp=disnake.utils.utcnow(),
            )
            embed.set_footer(text="INTERPOLOV & INTOLERANCE • Настройка профиля")
            delivered = True
            try:
                await member.send(embed=embed)
            except disnake.DiscordException:
                delivered = False
                logging.warning("Could not send profile server reminder to %s", member.id)
            await self.bot.database.record_profile_server_reminder(
                member.id, delivered
            )
            await asyncio.sleep(0.25)

    @sync_profile_servers_from_roles.before_loop
    async def before_profile_server_role_sync(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)

    @commands.slash_command(
        name="myinfo",
        description="Показать ваш профиль",
        guild_ids=GUILD_IDS,
    )
    async def myinfo(self, inter: disnake.ApplicationCommandInteraction) -> None:
        profile = await self.bot.database.get_user(inter.author.id)
        if profile is None:
            embed = disnake.Embed(
                title="Профиль пока не создан",
                description="Попросите администратора добавить вас командой `/user add`.",
                color=disnake.Color.gold(),
            )
            embed.set_thumbnail(url=inter.author.display_avatar.url)
            await inter.response.send_message(embed=embed, ephemeral=True)
            return

        if not profile["is_active"]:
            warning_count = await self.bot.database.get_active_disciplinary_warning_count(
                inter.author.id
            )
            await inter.response.send_message(
                embed=profile_embed(
                    inter.author, profile, warning_count=warning_count
                ),
                ephemeral=True,
            )
            return

        weekly_voice_seconds, total_voice_seconds, warning_count = await asyncio.gather(
            self.bot.database.get_weekly_voice_seconds(inter.author.id),
            self.bot.database.get_total_voice_seconds(inter.author.id),
            self.bot.database.get_active_disciplinary_warning_count(inter.author.id),
        )
        server_rows = [
            row for row in await self.bot.database.get_game_servers(inter.guild_id)
            if row["role_id"] is not None and inter.guild.get_role(row["role_id"]) is not None
        ][:25]
        if not server_rows:
            await inter.response.send_message(
                "Игровые серверы ещё не настроены. Сообщите администратору.", ephemeral=True
            )
            return
        await inter.response.send_message(
            embed=profile_embed(
                inter.author,
                profile,
                weekly_voice_seconds,
                total_voice_seconds,
                warning_count,
            ),
            view=UserServerView(self.bot, inter.author, profile["server_name"], server_rows),
            ephemeral=True,
        )

    @commands.slash_command(
        name="user",
        description="Управление профилями пользователей",
        guild_ids=GUILD_IDS,
    )
    async def user(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @user.sub_command(name="add", description="Добавить пользователя в базу данных")
    async def user_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Участник Discord"),
        nickname: str = commands.Param(description="Игровой ник", max_length=100),
        server: str | None = commands.Param(default=None, description="Игровой сервер", max_length=100),
        bio: str | None = commands.Param(default=None, description="Описание профиля", max_length=500),
    ) -> None:
        config = await self.bot.database.get_application_config(inter.guild_id) if inter.guild_id else None
        if config is None:
            await inter.response.send_message(
                "Сначала настройте семейные роли командой `/applications setup`.",
                ephemeral=True,
            )
            return
        family_row = await self.bot.database.get_family_by_name(
            inter.guild_id, FAMILY_NAME
        )
        if family_row is None or family_row["role_id"] is None:
            await inter.response.send_message(
                "Роль объединённой семьи не настроена. Используйте `/applications setup`.",
                ephemeral=True,
            )
            return
        role_id = family_row["role_id"]
        family_role_ids = {
            row["role_id"] for row in await self.bot.database.get_families(inter.guild_id)
            if row["role_id"] is not None
        }
        if not member.guild_permissions.administrator and not any(
            role.id == role_id for role in member.roles
        ):
            await inter.response.send_message(
                f"У пользователя нет роли семьи **{FAMILY_NAME}**.",
                ephemeral=True,
            )
            return
        await self.bot.database.save_users([member], role_ids=family_role_ids)
        profile = await self.bot.database.update_user_profile(
            member.id,
            game_nickname=nickname,
            server_name=server,
            bio=bio,
            added_by=inter.author.id,
            reactivate=True,
        )
        profile = await self.bot.database.set_user_family(member.id, FAMILY_NAME)
        self.bot.dispatch("voice_eligibility_changed")
        await inter.response.send_message(
            content=f"✅ Профиль {member.mention} добавлен или обновлён.",
            embed=profile_embed(member, profile) if profile else None,
            ephemeral=True,
        )
        await send_admin_log(self.bot, title="👤 Пользователь добавлен", description=f"Администратор: {inter.author.mention}\nПользователь: {member.mention} (`{member.id}`)", color=disnake.Color.green())

    @user.sub_command(name="edit", description="Изменить профиль пользователя")
    async def user_edit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Участник Discord"),
        nickname: str | None = commands.Param(default=None, description="Игровой ник", max_length=100),
        server: str | None = commands.Param(default=None, description="Игровой сервер", max_length=100),
        status: str | None = commands.Param(
            default=None,
            description="Статус профиля",
            choices=["Активен", "Неактивен"],
        ),
        bio: str | None = commands.Param(default=None, description="Описание профиля", max_length=500),
    ) -> None:
        profile = await self.bot.database.get_user(member.id)
        if profile is None:
            await inter.response.send_message(
                "Пользователь отсутствует в базе. Сначала используйте `/user add`.",
                ephemeral=True,
            )
            return

        eligibility_changed = False
        if status == "Неактивен":
            eligibility_changed = bool(profile["is_active"])
            await self.bot.database.deactivate_user(
                member.id,
                f"Деактивирован администратором {inter.author}",
            )
            profile = await self.bot.database.update_user_profile(
                member.id,
                game_nickname=nickname,
                server_name=server,
                profile_status=status,
                bio=bio,
            )
        else:
            reactivate = status == "Активен" and not profile["is_active"]
            if reactivate and not member.guild_permissions.administrator:
                family_role_ids = set(TARGET_ROLE_IDS)
                family_role_ids.update(
                    row["role_id"] for row in await self.bot.database.get_families(GUILD_ID)
                    if row["role_id"] is not None
                )
                if not any(role.id in family_role_ids for role in member.roles):
                    await inter.response.send_message(
                        "Нельзя реактивировать профиль без семейной роли. "
                        "Сначала верните участнику роль семьи.",
                        ephemeral=True,
                    )
                    return
            eligibility_changed = reactivate
            profile = await self.bot.database.update_user_profile(
                member.id,
                game_nickname=nickname,
                server_name=server,
                profile_status=status,
                bio=bio,
                reactivate=reactivate,
            )
        if eligibility_changed:
            self.bot.dispatch("voice_eligibility_changed")
        await inter.response.send_message(
            content=f"✏️ Профиль {member.mention} обновлён. Для очистки поля укажите `-`.",
            embed=profile_embed(member, profile) if profile else None,
            ephemeral=True,
        )
        await send_admin_log(self.bot, title="✏️ Профиль изменён", description=f"Администратор: {inter.author.mention}\nПользователь: {member.mention} (`{member.id}`)")

    @user.sub_command(name="view", description="Посмотреть профиль пользователя")
    async def user_view(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Участник Discord"),
    ) -> None:
        profile = await self.bot.database.get_user(member.id)
        if profile is None:
            await inter.response.send_message("Пользователь отсутствует в базе.", ephemeral=True)
            return

        weekly_voice_seconds, total_voice_seconds, warning_count = await asyncio.gather(
            self.bot.database.get_weekly_voice_seconds(member.id),
            self.bot.database.get_total_voice_seconds(member.id),
            self.bot.database.get_active_disciplinary_warning_count(member.id),
        )
        await inter.response.send_message(
            embed=profile_embed(
                member,
                profile,
                weekly_voice_seconds,
                total_voice_seconds,
                warning_count,
            ),
            ephemeral=True,
        )
        await send_admin_log(self.bot, title="🔎 Просмотр профиля", description=f"Администратор: {inter.author.mention}\nПользователь: {member.mention} (`{member.id}`)")

    @user.sub_command(name="remove", description="Удалить пользователя из базы данных")
    async def user_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Участник Discord"),
        confirm: bool = commands.Param(description="Подтверждение удаления"),
    ) -> None:
        if not confirm:
            await inter.response.send_message("Удаление отменено.", ephemeral=True)
            return

        deactivated = await self.bot.database.deactivate_user(
            member.id, f"Деактивирован администратором {inter.author}"
        )
        text = (
            f"⛔ Профиль {member.mention} деактивирован. Запись сохранена в базе."
            if deactivated else "Пользователь отсутствует в базе или уже неактивен."
        )
        await inter.response.send_message(text, ephemeral=True)
        if deactivated:
            await send_admin_log(self.bot, title="⛔ Пользователь деактивирован", description=f"Администратор: {inter.author.mention}\nПользователь: {member} (`{member.id}`)\nПричина: **Удалён администратором**", color=disnake.Color.red())
