import asyncio
import logging
from typing import TYPE_CHECKING

import asyncpg
import disnake
from disnake.ext import commands

from access_control import has_family_role, member_access_level, promotion_approval_error
from cogs.family_ranks import family_rank_from_roles
from config import FAMILY_RANK_ROLE_IDS, GUILD_ID, HIGH_FAMQ_ROLE_ID, PROMOTION_LOG_CHANNEL_ID
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import InterpolovBot


class PromotionChoices(disnake.ui.View):
    def __init__(self, cog: "PromotionsCog", owner_id: int, ranks: list[str], servers, default_server):
        super().__init__(timeout=600)
        self.cog, self.owner_id = cog, owner_id
        self.target_rank: str | None = None
        self.server_name: str | None = default_server
        self.rank_select = disnake.ui.StringSelect(
            placeholder="На какой ранг повыситься?",
            options=[disnake.SelectOption(label=rank, value=rank) for rank in ranks],
        )
        self.server_select = disnake.ui.StringSelect(
            placeholder="Игровой сервер",
            options=[disnake.SelectOption(label="Сервер не указан", value="none", default=default_server is None)]
            + [disnake.SelectOption(label=row["name"], value=str(row["id"]),
                                    default=row["name"] == default_server) for row in servers],
        )
        self.servers = {str(row["id"]): row["name"] for row in servers}
        self.rank_select.callback = self.select_rank
        self.server_select.callback = self.select_server
        self.add_item(self.rank_select)
        self.add_item(self.server_select)

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.guild_id != GUILD_ID or inter.author.id != self.owner_id:
            await inter.response.send_message("Это форма другого участника.", ephemeral=True)
            return False
        return True

    async def select_rank(self, inter: disnake.MessageInteraction) -> None:
        self.target_rank = self.rank_select.values[0]
        for option in self.rank_select.options:
            option.default = option.value == self.target_rank
        await inter.response.edit_message(view=self)

    async def select_server(self, inter: disnake.MessageInteraction) -> None:
        value = self.server_select.values[0]
        self.server_name = self.servers.get(value)
        for option in self.server_select.options:
            option.default = option.value == value
        await inter.response.edit_message(view=self)

    @disnake.ui.button(label="Заполнить заявку", style=disnake.ButtonStyle.success, row=2)
    async def fill_request(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        if self.target_rank is None:
            await inter.response.send_message("Сначала выберите желаемый ранг.", ephemeral=True)
            return
        await inter.response.send_modal(PromotionModal(self.cog, self.target_rank, self.server_name))


class PromotionModal(disnake.ui.Modal):
    def __init__(self, cog: "PromotionsCog", target_rank: str, server_name: str | None):
        self.cog, self.target_rank, self.server_name = cog, target_rank, server_name
        super().__init__(
            title=f"Повышение: {target_rank}",
            components=[
                disnake.ui.TextInput(label="Что вы сделали", custom_id="achievements",
                                    style=disnake.TextInputStyle.paragraph, max_length=1500),
                disnake.ui.TextInput(label="Доказательства (при наличии)", custom_id="evidence",
                                    style=disnake.TextInputStyle.paragraph, required=False, max_length=1000),
            ],
            timeout=600,
        )

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        await self.cog.submit_request(
            inter, self.target_rank, self.server_name,
            inter.text_values["achievements"].strip(), inter.text_values["evidence"].strip(),
        )


def promotion_embed(request) -> disnake.Embed:
    status = request["status"]
    title, color = {
        "pending": ("📈 Заявка на повышение", disnake.Color.gold()),
        "approved": ("✅ Повышение одобрено", disnake.Color.green()),
        "declined": ("❌ Повышение отклонено", disnake.Color.red()),
    }.get(status, ("Заявка отменена", disnake.Color.greyple()))
    embed = disnake.Embed(title=title, description=f"Заявитель: <@{request['discord_id']}>",
                          color=color, timestamp=request["created_at"])
    embed.add_field(name="Повышение", value=f"**{request['from_rank']} → {request['target_rank']}**", inline=True)
    embed.add_field(name="Игровой сервер", value=request["server_name"] or "Не указан", inline=True)
    for offset in range(0, len(request["achievements"]), 1024):
        embed.add_field(name="Что сделано" if offset == 0 else "Продолжение",
                        value=request["achievements"][offset:offset + 1024], inline=False)
    embed.add_field(name="Доказательства", value=request["evidence"] or "Не указаны", inline=False)
    if status in {"approved", "declined"}:
        embed.add_field(name="Решение", value=f"<@{request['reviewed_by']}>", inline=False)
    embed.set_footer(text=f"INTERPOLOV • Повышение #{request['id']}")
    return embed


class PromotionsCog(commands.Cog):
    def __init__(self, bot: "InterpolovBot") -> None:
        self.bot = bot
        self.review_slots = asyncio.Semaphore(3)

    async def applicant_error(self, member: disnake.Member) -> str | None:
        if member.guild.id != GUILD_ID or member.bot:
            return "Повышения доступны только участникам основного сервера."
        profile = await self.bot.database.get_user(member.id)
        if profile is None or not profile["is_active"] or not await has_family_role(self.bot, member):
            return "Для повышения нужен активный профиль участника семьи."
        if await self.bot.database.get_mafia_saved_access(GUILD_ID, member.id):
            return "Дождитесь восстановления ролей после Mafia."
        return None

    async def start_request(self, inter: disnake.MessageInteraction) -> None:
        if inter.guild_id != GUILD_ID or not isinstance(inter.author, disnake.Member):
            return
        await inter.response.defer(with_message=True, ephemeral=True)
        try:
            member = await inter.guild.fetch_member(inter.author.id)
            error = await self.applicant_error(member)
            if error:
                await inter.edit_original_response(error)
                return
            current = family_rank_from_roles({role.id for role in member.roles})
            ranks = list(FAMILY_RANK_ROLE_IDS)
            available = [rank for rank in ranks[ranks.index(current) + 1:]
                         if inter.guild.get_role(FAMILY_RANK_ROLE_IDS[rank]) is not None]
            if not available:
                await inter.edit_original_response("Доступных рангов выше вашего нет.")
                return
            servers = (await self.bot.database.get_game_servers(GUILD_ID))[:24]
            profile = await self.bot.database.get_user(member.id)
            default = next((row["name"] for row in servers if row["name"] == profile["server_name"]), None)
            await inter.edit_original_response(
                f"Ваш ранг: **{current}**. Выберите желаемый ранг и игровой сервер.",
                view=PromotionChoices(self, member.id, available, servers, default),
            )
        except (disnake.DiscordException, asyncpg.PostgresError, OSError, RuntimeError):
            logging.exception("Could not open promotion form")
            await inter.edit_original_response("Не удалось открыть форму. Попробуйте позже.")

    async def notification_role_ids(self, guild: disnake.Guild, server_name: str | None) -> list[int]:
        staff = await self.bot.database.get_application_staff_roles(guild.id)
        selected = [row for row in staff if server_name and row["server_name"] == server_name
                    and guild.get_role(row["role_id"]) is not None]
        high = {row["role_id"] for row in selected if row["access_level"] == 4}
        recruiters = {row["role_id"] for row in selected if row["access_level"] == 2}
        if not recruiters:
            recruiters = {row["member_role_id"] for row in await self.bot.database.get_department_configs(guild.id)
                          if row["department"] == "recruiters"}
        if not server_name:
            recruiters.update(row["role_id"] for row in staff if row["access_level"] == 2)
        return sorted((high or {HIGH_FAMQ_ROLE_ID}) | recruiters
                      | {FAMILY_RANK_ROLE_IDS["deputy leader"]})

    async def submit_request(self, inter, target_rank, server_name, achievements, evidence) -> None:
        if inter.guild_id != GUILD_ID or not isinstance(inter.author, disnake.Member):
            return
        await inter.response.defer(ephemeral=True)
        request = None
        try:
            member = await inter.guild.fetch_member(inter.author.id)
            error = await self.applicant_error(member)
            if error:
                await inter.edit_original_response(error)
                return
            ranks = list(FAMILY_RANK_ROLE_IDS)
            current = family_rank_from_roles({role.id for role in member.roles})
            if target_rank not in ranks or ranks.index(target_rank) <= ranks.index(current):
                await inter.edit_original_response("Выберите ранг выше вашего текущего.")
                return
            if inter.guild.get_role(FAMILY_RANK_ROLE_IDS[target_rank]) is None:
                await inter.edit_original_response("Роль выбранного ранга больше не существует.")
                return
            servers = await self.bot.database.get_game_servers(GUILD_ID)
            if server_name is not None and server_name not in {row["name"] for row in servers}:
                await inter.edit_original_response("Список серверов изменился. Откройте форму заново.")
                return
            if not achievements:
                await inter.edit_original_response("Опишите, что вы сделали для повышения.")
                return
            role_ids = await self.notification_role_ids(inter.guild, server_name)
            channel = self.bot.get_channel(PROMOTION_LOG_CHANNEL_ID)
            if channel is None:
                channel = await self.bot.fetch_channel(PROMOTION_LOG_CHANNEL_ID)
            if not isinstance(channel, (disnake.TextChannel, disnake.Thread)) or channel.guild.id != GUILD_ID:
                await inter.edit_original_response("Канал повышений недоступен.")
                return
            request = await self.bot.database.create_promotion_request(
                GUILD_ID, member.id, current, target_rank, server_name, achievements, evidence,
            )
            message = await channel.send(
                content=" ".join(f"<@&{role_id}>" for role_id in role_ids),
                embed=promotion_embed(request),
                components=[
                    disnake.ui.Button(label="Одобрить", style=disnake.ButtonStyle.success,
                                      custom_id=f"promotion:approved:{request['id']}"),
                    disnake.ui.Button(label="Отклонить", style=disnake.ButtonStyle.danger,
                                      custom_id=f"promotion:declined:{request['id']}"),
                ],
                allowed_mentions=disnake.AllowedMentions(
                    everyone=False, users=False, replied_user=False,
                    roles=[disnake.Object(id=role_id) for role_id in role_ids],
                ),
            )
            await self.bot.database.set_promotion_log_message(request["id"], message.id)
        except asyncpg.UniqueViolationError:
            await inter.edit_original_response("У вас уже есть заявка на повышение на рассмотрении.")
            return
        except (disnake.DiscordException, asyncpg.PostgresError, OSError, RuntimeError):
            logging.exception("Could not publish promotion request")
            if request is not None:
                try:
                    await self.bot.database.cancel_unpublished_promotion(request["id"])
                except (asyncpg.PostgresError, OSError, RuntimeError):
                    logging.exception("Could not cancel unpublished promotion %s", request["id"])
            await inter.edit_original_response("Не удалось отправить заявку. Попробуйте позже.")
            return
        await inter.edit_original_response(f"✅ Заявка **#{request['id']}** на ранг **{target_rank}** отправлена.")

    @commands.Cog.listener("on_button_click")
    async def review_button(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id or ""
        if not custom_id.startswith("promotion:") or inter.guild_id != GUILD_ID:
            return
        if not isinstance(inter.author, disnake.Member) or inter.channel_id != PROMOTION_LOG_CHANNEL_ID:
            return
        try:
            _, status, raw_id = custom_id.split(":")
            request_id = int(raw_id)
        except ValueError:
            return
        if status not in {"approved", "declined"}:
            return
        await inter.response.defer(with_message=True, ephemeral=True)
        try:
            reviewer = await inter.guild.fetch_member(inter.author.id)
            if await member_access_level(self.bot, reviewer) < 4:
                await inter.edit_original_response("Рассматривать повышение можно с уровня допуска 4.")
                return
            async with self.review_slots, self.bot.database.promotion_review_lock(request_id) as connection:
                request = await self.bot.database.get_promotion_request(connection, GUILD_ID, request_id)
                if request is None or request["log_message_id"] != inter.message.id:
                    await inter.edit_original_response("Заявка не найдена или это не её сообщение.")
                    return
                if request["status"] != "pending":
                    await self.update_message(inter.message, request)
                    await inter.edit_original_response("Эта заявка уже обработана.")
                    return
                if status == "approved":
                    error = await self.approve_roles(connection, inter.guild, reviewer, request)
                    if error:
                        await inter.edit_original_response(error)
                        return
                elif request["role_change_started"]:
                    # An HTTP timeout may have applied the rank. Confirm before declining.
                    member = await inter.guild.fetch_member(request["discord_id"])
                    if any(role.id == FAMILY_RANK_ROLE_IDS[request["target_rank"]] for role in member.roles):
                        await inter.edit_original_response("Роль уже выдана при предыдущей попытке. Завершите одобрение заявки.")
                        return
                request = await self.bot.database.finish_promotion_request(
                    connection, request_id, status, reviewer.id,
                )
            if request is None:
                await inter.edit_original_response("Эта заявка уже обработана.")
                return
        except (disnake.DiscordException, asyncpg.PostgresError, OSError, RuntimeError):
            logging.exception("Could not review promotion %s", request_id)
            await inter.edit_original_response(
                "Не удалось завершить обработку. Проверьте роли и повторите нажатие: "
                "бот проверит результат предыдущей попытки."
            )
            return
        await self.update_message(inter.message, request)
        await inter.edit_original_response("✅ Повышение одобрено." if status == "approved" else "Заявка отклонена.")
        await send_admin_log(
            self.bot, title="📈 Решение по повышению",
            description=f"Заявка: `#{request_id}` · {status}\nУчастник: <@{request['discord_id']}>\n"
                        f"Ранг: **{request['target_rank']}**\nРассмотрел: {reviewer.mention}",
        )

    async def approve_roles(self, connection, guild, reviewer, request) -> str | None:
        # Refresh both members after acquiring the lock; cached roles may be outdated.
        reviewer = await guild.fetch_member(reviewer.id)
        applicant = await guild.fetch_member(request["discord_id"])
        error = await promotion_approval_error(self.bot, reviewer, applicant, request["target_rank"])
        if error:
            return error
        error = await self.applicant_error(applicant)
        if error:
            return error
        if await self.bot.database.get_mafia_saved_access(GUILD_ID, reviewer.id):
            return "Дождитесь восстановления ваших ролей после Mafia."
        target_role = guild.get_role(FAMILY_RANK_ROLE_IDS[request["target_rank"]])
        rank_role_ids = set(FAMILY_RANK_ROLE_IDS.values())
        held_rank_ids = {role.id for role in applicant.roles} & rank_role_ids
        if request["role_change_started"] and held_rank_ids == {target_role.id}:
            return None
        current = family_rank_from_roles({role.id for role in applicant.roles})
        ranks = list(FAMILY_RANK_ROLE_IDS)
        if ranks.index(request["target_rank"]) <= ranks.index(current):
            return "Ранг заявителя уже изменился. Повышение должно быть выше текущего ранга."
        bot_member = await guild.fetch_member(self.bot.user.id)
        changed_roles = [role for role in applicant.roles if role.id in rank_role_ids] + [target_role]
        if (
            not bot_member.guild_permissions.manage_roles
            or applicant.top_role >= bot_member.top_role
            or any(role.managed or role >= bot_member.top_role for role in changed_roles)
        ):
            return "Бот не может изменить эти роли. Проверьте право «Управлять ролями» и иерархию Discord. Заявка остаётся открытой."
        roles = [role for role in applicant.roles if not role.is_default() and role.id not in rank_role_ids]
        roles.append(target_role)
        await self.bot.database.start_promotion_role_change(connection, request["id"], reviewer.id)
        await applicant.edit(roles=roles, reason=f"Повышение #{request['id']}; одобрил {reviewer.id}")
        updated = await guild.fetch_member(applicant.id)
        if {role.id for role in updated.roles} & rank_role_ids != {target_role.id}:
            return "Discord не подтвердил смену ранга. Заявка остаётся открытой; проверьте роли и повторите."
        return None

    async def update_message(self, message: disnake.Message, request) -> None:
        try:
            await message.edit(embed=promotion_embed(request), components=[],
                               allowed_mentions=disnake.AllowedMentions.none())
        except disnake.DiscordException:
            logging.exception("Could not update promotion message %s", request["id"])
