import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

import asyncpg
import disnake
from disnake.ext import commands

from config import GUILD_IDS
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import IntoleranceBot


STATUS_COLORS = {
    "Активен": disnake.Color.green(),
    "В отпуске": disnake.Color.gold(),
    "Неактивен": disnake.Color.red(),
}


def format_date(value: datetime | None) -> str:
    if value is None:
        return "—"
    return f"<t:{int(value.timestamp())}:F>"


def profile_embed(
    user: disnake.User | disnake.Member,
    profile: asyncpg.Record,
    weekly_voice_seconds: int | None = None,
    total_voice_seconds: int | None = None,
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
    ) -> None:
        self.bot = bot
        self.profile_owner = owner
        self.owner_id = owner.id
        selected = {
            server.strip()
            for server in (current_servers or "").split(",")
            if server.strip()
        }
        options = [
            disnake.SelectOption(
                label="Seattle", value="Seattle", emoji="🌲", default="Seattle" in selected
            ),
            disnake.SelectOption(
                label="Orlando", value="Orlando", emoji="🌴", default="Orlando" in selected
            ),
        ]
        super().__init__(
            placeholder="Выберите игровой сервер",
            min_values=1,
            max_values=2,
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
        weekly_voice_seconds, total_voice_seconds = await asyncio.gather(
            self.bot.database.get_weekly_voice_seconds(self.owner_id),
            self.bot.database.get_total_voice_seconds(self.owner_id),
        )
        for option in self.options:
            option.default = option.value in self.values
        await inter.response.edit_message(
            content=f"✅ Сервер обновлён: **{server_name}**",
            embed=profile_embed(
                self.profile_owner, profile, weekly_voice_seconds, total_voice_seconds
            ) if profile else None,
            view=self.view,
        )


class UserServerView(disnake.ui.View):
    def __init__(
        self,
        bot: "IntoleranceBot",
        owner: disnake.Member,
        current_servers: str | None,
    ) -> None:
        super().__init__(timeout=300)
        self.add_item(ServerSelect(bot, owner, current_servers))



class UsersCog(commands.Cog):
    def __init__(self, bot: "IntoleranceBot") -> None:
        self.bot = bot

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
            await inter.response.send_message(
                embed=profile_embed(inter.author, profile), ephemeral=True
            )
            return

        weekly_voice_seconds, total_voice_seconds = await asyncio.gather(
            self.bot.database.get_weekly_voice_seconds(inter.author.id),
            self.bot.database.get_total_voice_seconds(inter.author.id),
        )
        await inter.response.send_message(
            embed=profile_embed(
                inter.author, profile, weekly_voice_seconds, total_voice_seconds
            ),
            view=UserServerView(self.bot, inter.author, profile["server_name"]),
            ephemeral=True,
        )

    @commands.slash_command(
        name="user",
        description="Управление профилями пользователей",
        guild_ids=GUILD_IDS,
        default_member_permissions=disnake.Permissions(administrator=True),
    )
    async def user(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @user.sub_command(name="add", description="Добавить пользователя в базу данных")
    @commands.has_permissions(administrator=True)
    async def user_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Участник Discord"),
        family: str = commands.Param(
            description="Семья участника",
            choices=["INTERPOLOV", "INTOLERANCE"],
        ),
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
        role_id = config[
            "interpolov_role_id" if family == "INTERPOLOV" else "intolerance_role_id"
        ]
        family_role_ids = {config["interpolov_role_id"], config["intolerance_role_id"]}
        if not member.guild_permissions.administrator and not any(
            role.id == role_id for role in member.roles
        ):
            await inter.response.send_message(
                f"У пользователя нет роли семьи **{family}**.",
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
        profile = await self.bot.database.set_user_family(member.id, family)
        await inter.response.send_message(
            content=f"✅ Профиль {member.mention} добавлен или обновлён.",
            embed=profile_embed(member, profile) if profile else None,
            ephemeral=True,
        )
        await send_admin_log(self.bot, title="👤 Пользователь добавлен", description=f"Администратор: {inter.author.mention}\nПользователь: {member.mention} (`{member.id}`)", color=disnake.Color.green())

    @user.sub_command(name="edit", description="Изменить профиль пользователя")
    @commands.has_permissions(administrator=True)
    async def user_edit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Участник Discord"),
        nickname: str | None = commands.Param(default=None, description="Игровой ник", max_length=100),
        server: str | None = commands.Param(default=None, description="Игровой сервер", max_length=100),
        status: str | None = commands.Param(
            default=None,
            description="Статус профиля",
            choices=["Активен", "В отпуске", "Неактивен"],
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

        profile = await self.bot.database.update_user_profile(
            member.id,
            game_nickname=nickname,
            server_name=server,
            profile_status=status,
            bio=bio,
        )
        await inter.response.send_message(
            content=f"✏️ Профиль {member.mention} обновлён. Для очистки поля укажите `-`.",
            embed=profile_embed(member, profile) if profile else None,
            ephemeral=True,
        )
        await send_admin_log(self.bot, title="✏️ Профиль изменён", description=f"Администратор: {inter.author.mention}\nПользователь: {member.mention} (`{member.id}`)")

    @user.sub_command(name="view", description="Посмотреть профиль пользователя")
    @commands.has_permissions(administrator=True)
    async def user_view(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Участник Discord"),
    ) -> None:
        profile = await self.bot.database.get_user(member.id)
        if profile is None:
            await inter.response.send_message("Пользователь отсутствует в базе.", ephemeral=True)
            return

        weekly_voice_seconds, total_voice_seconds = await asyncio.gather(
            self.bot.database.get_weekly_voice_seconds(member.id),
            self.bot.database.get_total_voice_seconds(member.id),
        )
        await inter.response.send_message(
            embed=profile_embed(
                member, profile, weekly_voice_seconds, total_voice_seconds
            ),
            ephemeral=True,
        )
        await send_admin_log(self.bot, title="🔎 Просмотр профиля", description=f"Администратор: {inter.author.mention}\nПользователь: {member.mention} (`{member.id}`)")

    @user.sub_command(name="remove", description="Удалить пользователя из базы данных")
    @commands.has_permissions(administrator=True)
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
