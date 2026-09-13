import asyncio
import logging
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING

import asyncpg
import disnake
from disnake.ext import commands, tasks

from config import GIVEAWAY_CHANNEL_ID, GUILD_ID, GUILD_IDS

if TYPE_CHECKING:
    from bot import InterpolovBot


CLAIM_CHOICES = {
    "Без подтверждения": 0,
    "15 минут": 15,
    "30 минут": 30,
    "1 час": 60,
}

STATUS_NAMES = {
    "active": "Принимает участников",
    "drawing": "Определяет победителей",
    "claiming": "Ожидает подтверждения",
    "finished": "Завершён",
    "cancelled": "Отменён",
}


def giveaway_components(giveaway_id: int, status: str) -> list[disnake.ui.Button]:
    if status == "active":
        return [
            disnake.ui.Button(
                label="Участвовать",
                emoji="🎉",
                style=disnake.ButtonStyle.success,
                custom_id=f"giveaway:join:{giveaway_id}",
            ),
            disnake.ui.Button(
                label="Отказаться",
                emoji="↩️",
                style=disnake.ButtonStyle.secondary,
                custom_id=f"giveaway:leave:{giveaway_id}",
            ),
        ]
    if status == "claiming":
        return [
            disnake.ui.Button(
                label="Забрать приз",
                emoji="🎁",
                style=disnake.ButtonStyle.success,
                custom_id=f"giveaway:claim:{giveaway_id}",
            )
        ]
    return []


class GiveawaysCog(commands.Cog):
    def __init__(self, bot: "InterpolovBot") -> None:
        self.bot = bot
        self._giveaway_locks: dict[int, asyncio.Lock] = {}
        self.finish_giveaways.start()

    def cog_unload(self) -> None:
        self.finish_giveaways.cancel()

    def giveaway_lock(self, giveaway_id: int) -> asyncio.Lock:
        return self._giveaway_locks.setdefault(giveaway_id, asyncio.Lock())

    async def get_giveaway_channel(
        self,
    ) -> disnake.TextChannel | disnake.Thread | None:
        channel = self.bot.get_channel(GIVEAWAY_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(GIVEAWAY_CHANNEL_ID)
            except disnake.DiscordException:
                logging.exception(
                    "Could not fetch giveaway channel %s", GIVEAWAY_CHANNEL_ID
                )
                return None
        if not isinstance(channel, (disnake.TextChannel, disnake.Thread)):
            logging.error(
                "Giveaway channel %s is not a text channel", GIVEAWAY_CHANNEL_ID
            )
            return None
        if channel.guild.id != GUILD_ID:
            logging.error(
                "Giveaway channel %s does not belong to guild %s",
                GIVEAWAY_CHANNEL_ID,
                GUILD_ID,
            )
            return None
        return channel

    async def build_giveaway_embed(self, giveaway: asyncpg.Record) -> disnake.Embed:
        giveaway_id = giveaway["id"]
        status = giveaway["status"]
        entry_count = await self.bot.database.get_giveaway_entry_count(giveaway_id)
        winners = await self.bot.database.get_current_giveaway_winners(giveaway_id)

        if status == "active":
            description = (
                "Нажмите **«Участвовать»**, чтобы принять участие.\n"
                "Точный момент завершения выбран случайно и остаётся скрытым."
            )
            color = disnake.Color.blurple()
        elif status == "drawing":
            description = (
                "⏳ **Розыгрыш истёк. Приём заявок завершён.**\n"
                "Бот определяет победителей."
            )
            color = disnake.Color.orange()
        elif status == "claiming":
            description = (
                "⏳ **Розыгрыш истёк. Приём заявок завершён.**\n"
                "Выбранным победителям необходимо подтвердить получение приза."
            )
            color = disnake.Color.gold()
        elif status == "cancelled":
            description = (
                "❌ **Розыгрыш отменён организатором.**\n"
                "Победитель не определялся."
            )
            color = disnake.Color.red()
        else:
            description = "⏳ **Розыгрыш истёк. Приём заявок завершён.**"
            color = disnake.Color.green() if winners else disnake.Color.orange()

        embed = disnake.Embed(
            title=f"🎁 Секретный розыгрыш #{giveaway_id}",
            description=description,
            color=color,
            timestamp=giveaway["created_at"],
        )
        embed.add_field(name="Приз", value=giveaway["prize"], inline=False)
        embed.add_field(
            name="Победителей",
            value=str(giveaway["winner_count"]),
            inline=True,
        )
        embed.add_field(name="Участников", value=str(entry_count), inline=True)

        required_role_id = giveaway["required_role_id"]
        embed.add_field(
            name="Условие участия",
            value=(
                f"<@&{required_role_id}>"
                if required_role_id
                else "Без обязательной роли"
            ),
            inline=False,
        )
        if status == "active":
            earliest = int(giveaway["earliest_end_at"].timestamp())
            latest = int(giveaway["latest_end_at"].timestamp())
            embed.add_field(
                name="Период завершения",
                value=f"Между <t:{earliest}:f> и <t:{latest}:f>",
                inline=False,
            )
        elif winners:
            winner_lines = []
            for winner in winners:
                if giveaway["claim_minutes"] == 0:
                    confirmation = ""
                elif winner["confirmed_at"] is not None:
                    confirmation = " — ✅ приз подтверждён"
                else:
                    confirmation = " — ⏳ ожидается подтверждение"
                winner_lines.append(f"<@{winner['discord_id']}>{confirmation}")
            embed.add_field(
                name="Победители",
                value="\n".join(winner_lines),
                inline=False,
            )
        elif status == "finished":
            embed.add_field(
                name="Результат",
                value="Подходящих участников для выбора победителя не найдено.",
                inline=False,
            )

        if status == "claiming" and giveaway["claim_deadline"] is not None:
            deadline = int(giveaway["claim_deadline"].timestamp())
            embed.add_field(
                name="Подтвердить до",
                value=f"<t:{deadline}:f> (<t:{deadline}:R>)",
                inline=False,
            )
        elif status in {"finished", "cancelled"} and giveaway["finished_at"] is not None:
            finished_at = int(giveaway["finished_at"].timestamp())
            embed.add_field(
                name="Завершён",
                value=f"<t:{finished_at}:f>",
                inline=False,
            )

        embed.set_footer(
            text=f"INTERPOLOV • {STATUS_NAMES[status]}"
        )
        return embed

    async def update_giveaway_message(self, giveaway: asyncpg.Record) -> None:
        channel = await self.get_giveaway_channel()
        if channel is None:
            return
        embed = await self.build_giveaway_embed(giveaway)
        components = giveaway_components(giveaway["id"], giveaway["status"])
        message = None
        if giveaway["message_id"] is not None:
            try:
                message = await channel.fetch_message(giveaway["message_id"])
            except disnake.NotFound:
                message = None
            except disnake.DiscordException:
                logging.exception(
                    "Could not fetch giveaway message %s", giveaway["message_id"]
                )
                return
        try:
            if message is None:
                message = await channel.send(embed=embed, components=components)
                await self.bot.database.set_giveaway_message(
                    giveaway["id"], message.id
                )
            else:
                await message.edit(embed=embed, components=components)
        except disnake.DiscordException:
            logging.exception(
                "Could not publish giveaway %s state", giveaway["id"]
            )

    async def eligible_candidates(
        self, giveaway: asyncpg.Record
    ) -> list[int] | None:
        guild = self.bot.get_guild(giveaway["guild_id"])
        if guild is None:
            logging.warning(
                "Guild %s is unavailable while drawing giveaway %s",
                giveaway["guild_id"],
                giveaway["id"],
            )
            return None

        required_role_id = giveaway["required_role_id"]
        eligible: list[int] = []
        for discord_id in await self.bot.database.get_giveaway_candidate_ids(
            giveaway["id"]
        ):
            member = guild.get_member(discord_id)
            if member is None:
                try:
                    member = await guild.fetch_member(discord_id)
                except disnake.NotFound:
                    continue
                except disnake.DiscordException:
                    logging.exception(
                        "Could not verify giveaway participant %s", discord_id
                    )
                    return None
            if member.bot:
                continue
            if required_role_id is not None and all(
                role.id != required_role_id for role in member.roles
            ):
                continue
            eligible.append(member.id)
        return eligible

    async def draw_giveaway(
        self, giveaway_id: int, mode: str = "scheduled"
    ) -> bool:
        async with self.giveaway_lock(giveaway_id):
            prepared = await self.bot.database.prepare_giveaway_draw(
                giveaway_id, GUILD_ID, mode
            )
            if prepared is None:
                return False
            giveaway, available_slots = prepared
            await self.update_giveaway_message(giveaway)

            candidates = await self.eligible_candidates(giveaway)
            if candidates is None:
                return False
            winner_total = min(available_slots, len(candidates))
            winner_ids = (
                secrets.SystemRandom().sample(candidates, winner_total)
                if winner_total
                else []
            )
            completed = await self.bot.database.complete_giveaway_draw(
                giveaway_id, winner_ids
            )
            if completed is None:
                return False
            await self.update_giveaway_message(completed)
            return True

    @tasks.loop(seconds=10)
    async def finish_giveaways(self) -> None:
        try:
            giveaway_ids = await self.bot.database.get_due_giveaway_ids()
        except Exception:
            logging.exception("Could not load due giveaways")
            return
        for giveaway_id in giveaway_ids:
            try:
                await self.draw_giveaway(giveaway_id)
            except Exception:
                logging.exception("Could not finish giveaway %s", giveaway_id)

    @finish_giveaways.before_loop
    async def before_finish_giveaways(self) -> None:
        await self.bot.wait_until_ready()

    @commands.slash_command(
        name="giveaway",
        description="Управление секретными розыгрышами",
        guild_ids=GUILD_IDS,
    )
    async def giveaway(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @giveaway.sub_command(name="create", description="Создать секретный розыгрыш")
    async def giveaway_create(
        self,
        inter: disnake.ApplicationCommandInteraction,
        prize: str = commands.Param(
            name="приз", description="Название приза", max_length=500
        ),
        minimum_minutes: int = commands.Param(
            name="минимум_минут", description="Не завершать раньше", ge=1, le=10080
        ),
        maximum_minutes: int = commands.Param(
            name="максимум_минут", description="Завершить не позднее", ge=2, le=10080
        ),
        winners: int = commands.Param(
            default=1, name="победителей", description="От 1 до 10", ge=1, le=10
        ),
        required_role: disnake.Role | None = commands.Param(
            default=None, name="роль", description="Обязательная роль участника"
        ),
        claim_minutes: int = commands.Param(
            default=0,
            name="подтверждение",
            description="Срок подтверждения победы",
            choices=CLAIM_CHOICES,
        ),
    ) -> None:
        prize = prize.strip()
        if not prize:
            await inter.response.send_message("Укажите название приза.", ephemeral=True)
            return
        if maximum_minutes <= minimum_minutes:
            await inter.response.send_message(
                "Максимальное время должно быть больше минимального.",
                ephemeral=True,
            )
            return
        channel = await self.get_giveaway_channel()
        if channel is None:
            await inter.response.send_message(
                "Не удалось найти настроенный канал розыгрышей или получить к нему доступ.",
                ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True)
        now = disnake.utils.utcnow()
        earliest = now + timedelta(minutes=minimum_minutes)
        latest = now + timedelta(minutes=maximum_minutes)
        random_seconds = secrets.randbelow(
            int((latest - earliest).total_seconds()) + 1
        )
        draw_at = earliest + timedelta(seconds=random_seconds)
        giveaway = None
        message = None
        try:
            giveaway = await self.bot.database.create_giveaway(
                GUILD_ID,
                GIVEAWAY_CHANNEL_ID,
                prize,
                inter.author.id,
                earliest,
                latest,
                draw_at,
                winners,
                required_role.id if required_role is not None else None,
                claim_minutes,
            )
            embed = await self.build_giveaway_embed(giveaway)
            message = await channel.send(
                embed=embed,
                components=giveaway_components(giveaway["id"], "active"),
            )
            await self.bot.database.set_giveaway_message(giveaway["id"], message.id)
        except (asyncpg.PostgresError, disnake.DiscordException):
            logging.exception("Could not create giveaway")
            if giveaway is not None:
                try:
                    cancelled = await self.bot.database.cancel_giveaway(
                        giveaway["id"], GUILD_ID, inter.author.id
                    )
                    if cancelled is not None and message is not None:
                        await message.edit(
                            embed=await self.build_giveaway_embed(cancelled),
                            components=[],
                        )
                except Exception:
                    logging.exception(
                        "Could not clean up failed giveaway %s", giveaway["id"]
                    )
            await inter.edit_original_response(
                "❌ Не удалось создать розыгрыш. Проверьте права бота в канале."
            )
            return
        await inter.edit_original_response(
            f"✅ Розыгрыш #{giveaway['id']} создан: {message.jump_url}"
        )

    @giveaway.sub_command(name="list", description="Показать открытые розыгрыши")
    async def giveaway_list(
        self, inter: disnake.ApplicationCommandInteraction
    ) -> None:
        rows = await self.bot.database.get_open_giveaways(GUILD_ID)
        if not rows:
            await inter.response.send_message(
                "Сейчас нет открытых розыгрышей.", ephemeral=True
            )
            return
        lines = []
        for row in rows[:10]:
            message_link = (
                f"https://discord.com/channels/{GUILD_ID}/{GIVEAWAY_CHANNEL_ID}/"
                f"{row['message_id']}"
                if row["message_id"] is not None
                else "сообщение создаётся"
            )
            prize = row["prize"]
            if len(prize) > 80:
                prize = prize[:77] + "…"
            lines.append(
                f"**#{row['id']}** — {prize} · "
                f"{STATUS_NAMES[row['status']]} · [открыть]({message_link})"
            )
        await inter.response.send_message("\n".join(lines), ephemeral=True)

    @giveaway.sub_command(name="info", description="Показать розыгрыш")
    async def giveaway_info(
        self,
        inter: disnake.ApplicationCommandInteraction,
        giveaway_id: int = commands.Param(
            name="номер", description="Номер розыгрыша", ge=1
        ),
    ) -> None:
        giveaway = await self.bot.database.get_giveaway(giveaway_id, GUILD_ID)
        if giveaway is None:
            await inter.response.send_message("Розыгрыш не найден.", ephemeral=True)
            return
        embed = await self.build_giveaway_embed(giveaway)
        await inter.response.send_message(embed=embed, ephemeral=True)

    @giveaway.sub_command(name="end", description="Завершить розыгрыш досрочно")
    async def giveaway_end(
        self,
        inter: disnake.ApplicationCommandInteraction,
        giveaway_id: int = commands.Param(
            name="номер", description="Номер розыгрыша", ge=1
        ),
    ) -> None:
        await inter.response.defer(ephemeral=True)
        completed = await self.draw_giveaway(giveaway_id, "end")
        if not completed:
            await inter.edit_original_response(
                "Розыгрыш не найден или уже не принимает участников."
            )
            return
        await inter.edit_original_response(
            f"✅ Розыгрыш #{giveaway_id} завершён досрочно."
        )

    @giveaway.sub_command(name="reroll", description="Перевыбрать победителей")
    async def giveaway_reroll(
        self,
        inter: disnake.ApplicationCommandInteraction,
        giveaway_id: int = commands.Param(
            name="номер", description="Номер розыгрыша", ge=1
        ),
    ) -> None:
        await inter.response.defer(ephemeral=True)
        completed = await self.draw_giveaway(giveaway_id, "reroll")
        if not completed:
            await inter.edit_original_response(
                "Перевыбор доступен только для завершённого розыгрыша или во время подтверждения."
            )
            return
        await inter.edit_original_response(
            f"✅ Победители розыгрыша #{giveaway_id} перевыбраны."
        )

    @giveaway.sub_command(name="cancel", description="Отменить розыгрыш")
    async def giveaway_cancel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        giveaway_id: int = commands.Param(
            name="номер", description="Номер розыгрыша", ge=1
        ),
    ) -> None:
        await inter.response.defer(ephemeral=True)
        async with self.giveaway_lock(giveaway_id):
            giveaway = await self.bot.database.cancel_giveaway(
                giveaway_id, GUILD_ID, inter.author.id
            )
            if giveaway is None:
                await inter.edit_original_response(
                    content="Розыгрыш не найден или уже завершён."
                )
                return
            await self.update_giveaway_message(giveaway)
        await inter.edit_original_response(
            content=f"✅ Розыгрыш #{giveaway_id} отменён."
        )

    @commands.Cog.listener("on_button_click")
    async def giveaway_button(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id or ""
        if not custom_id.startswith("giveaway:"):
            return
        try:
            action, raw_id = custom_id.split(":")[1:]
            giveaway_id = int(raw_id)
        except (ValueError, IndexError):
            await inter.response.send_message("Некорректная кнопка.", ephemeral=True)
            return
        if (
            inter.guild_id != GUILD_ID
            or inter.channel_id != GIVEAWAY_CHANNEL_ID
            or not isinstance(inter.author, disnake.Member)
        ):
            await inter.response.send_message(
                "Эта кнопка недоступна здесь.", ephemeral=True
            )
            return

        try:
            giveaway = await self.bot.database.get_giveaway(giveaway_id, GUILD_ID)
            if (
                giveaway is None
                or giveaway["message_id"] is None
                or inter.message.id != giveaway["message_id"]
            ):
                await inter.response.send_message(
                    "Этот розыгрыш не найден.", ephemeral=True
                )
                return

            if action == "join":
                required_role_id = giveaway["required_role_id"]
                if required_role_id is not None and all(
                    role.id != required_role_id for role in inter.author.roles
                ):
                    await inter.response.send_message(
                        f"Для участия требуется роль <@&{required_role_id}>.",
                        ephemeral=True,
                    )
                    return
                result = await self.bot.database.join_giveaway(
                    giveaway_id, GUILD_ID, inter.author.id
                )
                messages = {
                    "joined": "✅ Вы участвуете в розыгрыше.",
                    "already": "Вы уже участвуете в этом розыгрыше.",
                    "expired": "Розыгрыш уже истёк.",
                    "missing": "Розыгрыш не найден.",
                }
            elif action == "leave":
                result = await self.bot.database.leave_giveaway(
                    giveaway_id, GUILD_ID, inter.author.id
                )
                messages = {
                    "left": "Вы больше не участвуете в розыгрыше.",
                    "not_joined": "Вы не участвуете в этом розыгрыше.",
                    "expired": "Розыгрыш уже истёк.",
                    "missing": "Розыгрыш не найден.",
                }
            elif action == "claim":
                required_role_id = giveaway["required_role_id"]
                if required_role_id is not None and all(
                    role.id != required_role_id for role in inter.author.roles
                ):
                    await inter.response.send_message(
                        "Вы больше не соответствуете условиям розыгрыша.",
                        ephemeral=True,
                    )
                    return
                result = await self.bot.database.claim_giveaway(
                    giveaway_id, GUILD_ID, inter.author.id
                )
                messages = {
                    "claimed": "✅ Получение приза подтверждено.",
                    "already": "Вы уже подтвердили получение приза.",
                    "not_winner": "Эта кнопка доступна только победителям.",
                    "expired": "Время подтверждения уже истекло.",
                    "missing": "Розыгрыш не найден.",
                }
            else:
                await inter.response.send_message("Некорректная кнопка.", ephemeral=True)
                return

            await inter.response.send_message(messages[result], ephemeral=True)
            if result == "expired":
                await self.draw_giveaway(giveaway_id)
            else:
                async with self.giveaway_lock(giveaway_id):
                    refreshed = await self.bot.database.get_giveaway(
                        giveaway_id, GUILD_ID
                    )
                    if refreshed is not None:
                        await self.update_giveaway_message(refreshed)
        except Exception:
            logging.exception("Could not handle giveaway %s button", giveaway_id)
            if not inter.response.is_done():
                await inter.response.send_message(
                    "Не удалось обработать действие из-за внутренней ошибки.",
                    ephemeral=True,
                )
