import asyncio
import logging
from time import monotonic
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import asyncpg
import disnake
from disnake.ext import commands, tasks

from access_control import has_family_role, member_access_level
from config import GUILD_ID, GUILD_IDS
from cogs import mafia_game as rules
from cogs.mafia_rooms import MafiaRooms
from cogs.mafia_notifications import deliver_notifications

if TYPE_CHECKING:
    from bot import InterpolovBot


METRICS = {
    "Очки": "points", "Победы": "wins", "Сыгранные партии": "games",
    "Процент побед (от 5 игр)": "win_rate", "Победы за мафию": "mafia_wins",
    "Победы за город": "town_wins", "Выживания": "survived",
}
PERIODS = {"Всё время": 0, "7 дней": 7, "30 дней": 30}
ROLE_HELP = {
    "mafia": "Ночью выберите жертву. Решает большинство голосов мафии; при равенстве убийства нет.",
    "doctor": "Ночью защитите одного игрока. Можно лечить себя, в том числе несколько ночей подряд.",
    "commissioner": "Проверьте одного игрока за ночь. После проверки изменить цель нельзя.",
    "civilian": "Обсуждайте подозрения днём и голосуйте. Ночью действий нет.",
}


def game_embed(game: dict) -> disnake.Embed:
    state = game["state"]
    description = f"**{rules.PHASES[state['phase']]}** · Раунд {state['round']}\n{state['event']}"
    if state["paused"]:
        description += "\n⏸️ Игра на паузе."
    if state.get("setup_pending"):
        description += "\nПодготовка комнат и временное снятие админских ролей."
    if state.get('confirmation_pending'):
        description += '\nОжидаем подтверждения всех игроков. Если ЛС закрыты, нажмите кнопку на этой панели.'
    if state.get("room_error"):
        description += "\n⚠️ Проверьте права бота и положение его роли. Подготовка будет повторена."
    if game.get('restore_pending'):
        description += f"\n⏳ Ожидают возврата ролей: {game['restore_pending']}. Проверьте права и положение роли бота; восстановление повторяется."
    elif state["deadline"] is not None:
        description += f"\nКонец фазы: <t:{int(state['deadline'])}:R>"
    if state["winner"]:
        description += "\n**" + {"town": "Победил город!", "mafia": "Победила мафия!", "draw": "Ничья!"}[state["winner"]] + "**"
    if state.get('rooms_deleted'):
        description += '\nКомнаты партии удалены. Результат сохранён.'
    elif state.get('cleanup_at') is not None:
        description += f"\nУдаление комнат: <t:{int(state['cleanup_at'])}:R>. Результат доступен через `/mafia status game_id:{game['id']}`."
    embed = disnake.Embed(title=f"🎭 Мафия #{game['id']}", description=description, color=disnake.Color.dark_purple())
    players = []
    for uid, player in state["players"].items():
        text = f"{'🟢' if player['alive'] else '💀'} <@{uid}>"
        if state.get('confirmation_pending'):
            text += ' — ✅ готов' if uid in state.get('confirmations', []) else ' — ⏳ ждём подтверждения'
        if state["phase"] in {"finished", "cancelled"} and player["role"]:
            text += f" — {rules.ROLES[player['role']]}"
        players.append(text)
    embed.add_field(name=f"Игроки ({len(players)}/12)", value="\n".join(players) or "Пока никого", inline=False)
    embed.add_field(name="Организатор", value=f"<@{state['host_id']}>")
    if state["voice_id"] and not state.get('rooms_deleted'):
        embed.add_field(name="Обсуждение голосом", value=f"<#{state['voice_id']}>")
    if state["phase"] == "vote":
        embed.add_field(name="Голосование", value=f"Проголосовали: {len(state['votes'])}")
    if state["phase"] == "lobby":
        embed.add_field(
            name="Правила",
            value="5 игроков: 1 мафия; 6–8: 2 мафии; 9–12: 3 мафии. Также 1 доктор, 1 комиссар, остальные — мирные.\n"
                  "Ночь — 60 с, обсуждение — 120 с, голосование — 60 с. "
                  "При равенстве голосов никто не выбывает; можно воздержаться.\n"
                  "Вступить можно только с семейной ролью. Выбывшие не голосуют; ночью общий войс закрыт для речи. "
                  "После 20 раундов — ничья.",
            inline=False,
        )
    embed.set_footer(text="Моя роль / действие — видно только вам • Победа: 3 очка, поражение/ничья: 1")
    return embed


def game_buttons(game: dict) -> list[disnake.ui.Button]:
    state = game["state"]
    if state["phase"] not in rules.ACTIVE_PHASES:
        return []
    actions = [("role", "Моя роль / действие", disnake.ButtonStyle.primary)]
    if state["phase"] == "lobby":
        actions = [
            ("join", "Присоединиться", disnake.ButtonStyle.success),
            ("leave", "Выйти", disnake.ButtonStyle.secondary),
            ("start", "Начать", disnake.ButtonStyle.primary),
        ]
        if state.get('confirmation_pending'):
            actions = [
                ("confirm_start", "Подтверждаю участие", disnake.ButtonStyle.success),
                ("leave", "Выйти", disnake.ButtonStyle.secondary),
            ]
    else:
        actions.append(("pause", "Продолжить" if state["paused"] else "Пауза", disnake.ButtonStyle.secondary))
    actions.append(("stop", "Остановить", disnake.ButtonStyle.danger))
    return [disnake.ui.Button(label=label, style=style,
                             custom_id=f"mafia:{action}:{game['id']}" + (f":{state['token']}" if action == 'confirm_start' else ''))
            for action, label, style in actions]


def private_panel(game: dict, user_id: int) -> tuple[str, list]:
    state = game["state"]
    if state.get('setup_pending') and state['phase'] in rules.ACTIVE_PHASES:
        raise rules.GameError('Сначала бот должен подготовить комнаты и временно снять админские роли.')
    key = str(user_id)
    player = state["players"].get(key)
    if player is None or player["role"] is None:
        raise rules.GameError("Роль доступна участникам после начала игры.")
    role = player["role"]
    text = f"Ваша роль: **{rules.ROLES[role]}**.\n{ROLE_HELP[role]}"
    if role == "mafia":
        teammates = [f"<@{uid}>" for uid, other in state["players"].items() if other["role"] == "mafia"]
        text += "\nКоманда мафии: " + ", ".join(teammates)
        if state["phase"] == "night":
            choices = [f"<@{uid}> → <@{target}>" for uid, target in state["actions"].items()
                       if state["players"][uid]["role"] == "mafia"]
            if choices:
                text += "\nТекущие выборы команды:\n" + "\n".join(choices)
    if player["checks"]:
        text += "\nВаши проверки:\n" + "\n".join(f"<@{uid}>: {result}" for uid, result in player["checks"].items())
    if not player["alive"]:
        return text + "\nВы выбыли. Действия и голосование недоступны.", []
    if state["phase"] not in {"night", "vote"} or state["paused"]:
        return text + "\nСейчас нет доступных действий.", []
    if state["phase"] == "night" and role == "civilian":
        return text, []
    if state["phase"] == "night" and role == "commissioner" and key in state["actions"]:
        return text + "\nПроверка этой ночью уже использована.", []
    options = []
    for uid, target in state["players"].items():
        if not target["alive"]:
            continue
        if state["phase"] == "vote" and uid == key:
            continue
        if state["phase"] == "night":
            if role == "mafia" and target["role"] == "mafia":
                continue
            if role == "commissioner" and uid == key:
                continue
        options.append(disnake.SelectOption(label=f"{target['name']} · {uid}"[:100], value=uid))
    if state["phase"] == "vote":
        options.append(disnake.SelectOption(label="Воздержаться", value="skip"))
    if not options:
        return text, []
    previous = (state["votes"] if state["phase"] == "vote" else state["actions"]).get(key)
    if previous:
        text += "\nВаш выбор: " + ("воздержаться" if previous == "skip" else f"<@{previous}>")
    text += "\nВыберите цель. Меню доступно только в текущей фазе."
    return text, [disnake.ui.StringSelect(
        placeholder="Выберите действие", options=options,
        custom_id=f"mafia:act:{game['id']}:{state['token']}",
    )]


class MafiaCog(commands.Cog):
    def __init__(self, bot: "InterpolovBot") -> None:
        self.bot = bot
        self._message_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self._game_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self.rooms = MafiaRooms(bot)
        self._room_sync: dict[int, tuple[str, float]] = {}
        self.tick.start()

    def cog_unload(self) -> None:
        self.tick.cancel()

    async def cog_slash_command_error(
        self, inter: disnake.ApplicationCommandInteraction, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CheckFailure):
            return
        logging.error("Mafia command failed", exc_info=(type(error), error, error.__traceback__))
        message = "Не удалось выполнить команду мафии. Попробуйте позже."
        if inter.response.is_done():
            await inter.edit_original_response(message)
        else:
            await inter.response.send_message(message, ephemeral=True)

    async def sync_message(self, game_id: int, *, force: bool = False) -> None:
        lock = self._message_locks.setdefault(game_id, asyncio.Lock())
        async with lock:
            game = await self.bot.database.get_mafia_game(GUILD_ID, game_id)
            if game is None or (not force and game["message_revision"] >= game["revision"]):
                return
            if game['state'].get('rooms_deleted') or (
                game['state'].get('cleanup_at') is not None
                and disnake.utils.utcnow().timestamp() >= game['state']['cleanup_at']
            ):
                return
            if game['state']['phase'] not in rules.ACTIVE_PHASES:
                game['restore_pending'] = len(await self.bot.database.get_mafia_role_holds(game_id))
            channel = self.bot.get_channel(game["channel_id"])
            try:
                if channel is None:
                    channel = await self.bot.fetch_channel(game["channel_id"])
                if not isinstance(channel, disnake.TextChannel) or channel.guild.id != GUILD_ID:
                    return
                message = None
                if game["message_id"]:
                    try:
                        message = await channel.fetch_message(game["message_id"])
                    except disnake.NotFound:
                        pass
                kwargs = dict(content=None, embed=game_embed(game), components=game_buttons(game), allowed_mentions=disnake.AllowedMentions.none())
                if message is None:
                    message = await channel.send(**kwargs)
                else:
                    await message.edit(**kwargs)
                await self.bot.database.set_mafia_message(game_id, message.id, game["revision"])
            except disnake.DiscordException:
                logging.exception("Could not update mafia game %s panel", game_id)

    async def advance_game(self, game_id: int) -> None:
        async with self.bot.database.mafia_transaction(GUILD_ID, game_id) as game:
            if game is not None:
                rules.advance(game["state"], disnake.utils.utcnow().timestamp())

    def game_lock(self, game_id: int) -> asyncio.Lock:
        return self._game_locks.setdefault(game_id, asyncio.Lock())

    async def finish_game_resources(self, game: dict) -> None:
        try:
            await self.rooms.restore_admin_roles(game['id'])
        finally:
            # Role restoration, event delivery and cleanup must not block each other.
            if not game['state'].get('rooms_deleted'):
                cleanup_at = game['state'].get('cleanup_at')
                if cleanup_at is not None and disnake.utils.utcnow().timestamp() >= cleanup_at:
                    await self.rooms.delete_game_rooms(game)
                else:
                    if game['state'].get('rooms'):
                        await self.rooms.sync_permissions(game)
                    await self.rooms.send_events(game)

    async def maintain_game(self, game_id: int) -> None:
        game = await self.bot.database.get_mafia_game(GUILD_ID, game_id)
        if game is None:
            return
        state = game['state']
        ended = state['phase'] not in rules.ACTIVE_PHASES
        if ended:
            await self.finish_game_resources(game)
            return
        fingerprint = repr((state['phase'], state['paused'], state.get('setup_pending'),
                            [(uid, p['alive'], p['role']) for uid, p in state['players'].items()]))
        previous, checked_at = self._room_sync.get(game_id, ('', 0))
        due = previous != fingerprint or monotonic() - checked_at >= 30
        try:
            if due:
                if not ended:
                    game = await self.rooms.reconcile_players(game)
                    state = game['state']
                    ended = state['phase'] not in rules.ACTIVE_PHASES
                    if ended:
                        await self.finish_game_resources(game)
                        return
                if not ended:
                    game = await self.rooms.ensure_rooms(game)
                    if state['phase'] != 'lobby':
                        await self.rooms.suspend_admin_roles(game)
                if state.get('setup_pending') and not ended:
                    # Keep menus hidden until all Discord changes have succeeded.
                    prepared = dict(game, state=dict(game['state'], setup_pending=False, paused=False))
                    await self.rooms.sync_permissions(prepared)
                    async with self.bot.database.mafia_transaction(GUILD_ID, game_id) as current:
                        current['state'].update(
                            setup_pending=False, paused=False, remaining=None, room_error=False,
                            deadline=disnake.utils.utcnow().timestamp() + rules.DURATIONS['night'],
                        )
                        current['state']['token'] += 1
                    game = await self.bot.database.get_mafia_game(GUILD_ID, game_id)
                elif game['state'].get('rooms'):
                    await self.rooms.sync_permissions(game)
                    if state.get('room_error') and not ended:
                        game = await self.rooms.patch_state(game_id, room_error=False)
                self._room_sync[game_id] = (fingerprint, monotonic())
        except (disnake.DiscordException, rules.GameError):
            if not ended and state['phase'] != 'lobby':
                async with self.bot.database.mafia_transaction(GUILD_ID, game_id) as current:
                    current_state = current['state']
                    if not current_state['paused']:
                        rules.toggle_pause(current_state, disnake.utils.utcnow().timestamp())
                    current_state['room_error'] = True
            raise
        await self.rooms.send_events(game)

    @tasks.loop(seconds=5)
    async def tick(self) -> None:
        try:
            games = await self.bot.database.pending_mafia_games(GUILD_ID)
        except Exception:
            logging.exception("Could not load mafia games")
            return
        for row in games:
            try:
                async with self.game_lock(row['id']):
                    await self.maintain_game(row['id'])
                    await self.advance_game(row["id"])
                    await self.maintain_game(row['id'])
                    await self.sync_message(row["id"])
            except Exception:
                logging.exception("Could not update mafia game %s", row["id"])
                try:
                    await self.sync_message(row['id'])
                except Exception:
                    logging.exception('Could not display mafia recovery status')
        try:
            await deliver_notifications(self.bot)
        except Exception:
            logging.exception('Could not deliver Mafia personal notifications')

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener('on_ready')
    async def restore_mafia_panels(self) -> None:
        try:
            games = await self.bot.database.pending_mafia_games(GUILD_ID)
            for row in games:
                try:
                    async with self.game_lock(row['id']):
                        game = await self.bot.database.get_mafia_game(GUILD_ID, row['id'])
                        if game is not None and game['state']['phase'] in rules.ACTIVE_PHASES:
                            await self.sync_message(row['id'], force=True)
                except Exception:
                    logging.exception('Could not restore mafia panel %s', row['id'])
        except Exception:
            logging.exception('Could not load mafia panels for restoration')

    @commands.slash_command(name="mafia", description="Мафия: игры и рейтинги", guild_ids=GUILD_IDS)
    async def mafia(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @mafia.sub_command(name="create", description="Открыть набор в мафию (уровень 5)")
    async def create(
        self, inter: disnake.ApplicationCommandInteraction,
    ) -> None:
        if inter.guild_id != GUILD_ID or inter.guild is None:
            return
        if inter.guild.me is None:
            return
        permissions = inter.guild.me.guild_permissions
        if not permissions.manage_channels or not permissions.manage_roles:
            await inter.response.send_message("Боту нужны права «Управлять каналами» и «Управлять ролями».", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        try:
            state = rules.new_game(inter.author.id)
            state['rooms_enabled'] = True
            game_id = await self.bot.database.create_mafia_game(GUILD_ID, inter.channel_id, state)
        except asyncpg.UniqueViolationError:
            await inter.edit_original_response("На сервере уже есть активная игра. Используйте /mafia status.")
            return
        try:
            async with self.game_lock(game_id):
                await self.maintain_game(game_id)
                await self.sync_message(game_id)
        except (disnake.DiscordException, rules.GameError):
            logging.exception('Could not provision mafia rooms for game %s', game_id)
        game = await self.bot.database.get_mafia_game(GUILD_ID, game_id)
        if game and game["message_id"]:
            await inter.edit_original_response(f"✅ Создана категория мафии #{game_id}. Набор и события: <#{game['channel_id']}>.")
        else:
            await inter.edit_original_response("Игра сохранена, но комнаты ещё не готовы. Проверьте права и роль бота; создание будет повторено. Управление: /mafia status.")

    @mafia.sub_command(name="status", description="Показать игру и восстановить её панель")
    async def status(
        self, inter: disnake.ApplicationCommandInteraction,
        game_id: int | None = commands.Param(default=None, description="ID игры; по умолчанию — текущая", ge=1),
    ) -> None:
        await inter.response.defer(ephemeral=True)
        game = await self.bot.database.get_mafia_game(GUILD_ID, game_id)
        if game is None:
            await inter.edit_original_response("Игра не найдена.")
            return
        await self.sync_message(game["id"], force=True)
        if game['state']['phase'] not in rules.ACTIVE_PHASES:
            game['restore_pending'] = len(await self.bot.database.get_mafia_role_holds(game['id']))
        await inter.edit_original_response(embed=game_embed(game), components=game_buttons(game))

    @mafia.sub_command(name="top", description="Рейтинги игроков в мафию")
    async def top(
        self, inter: disnake.ApplicationCommandInteraction,
        metric: str = commands.Param(default="points", choices=METRICS, description="Вид рейтинга"),
        period: int = commands.Param(default=0, choices=PERIODS, description="Период"),
    ) -> None:
        await inter.response.defer(ephemeral=True)
        rows = await self.bot.database.mafia_leaderboard(GUILD_ID, metric, period)
        title = next(name for name, value in METRICS.items() if value == metric)
        period_name = next(name for name, value in PERIODS.items() if value == period)
        lines = [f"**{index}.** <@{row['discord_id']}> — **{row[metric]}{'%' if metric == 'win_rate' else ''}** · {row['games']} игр"
                 for index, row in enumerate(rows, 1)]
        embed = disnake.Embed(title=f"🏆 Мафия · {title}", description="\n".join(lines) or "Пока нет подходящих результатов.", color=disnake.Color.gold())
        embed.set_footer(text=f"{period_name} • Победа: 3 очка, поражение/ничья: 1 • Отменённые игры не учитываются")
        await inter.edit_original_response(embed=embed)

    @mafia.sub_command(name="stats", description="Статистика игрока в мафию")
    async def stats(
        self, inter: disnake.ApplicationCommandInteraction,
        user: disnake.Member | None = commands.Param(default=None, description="Игрок; по умолчанию — вы"),
    ) -> None:
        await inter.response.defer(ephemeral=True)
        user = user or inter.author
        rows = await self.bot.database.mafia_leaderboard(GUILD_ID, user_id=user.id)
        if not rows:
            await inter.edit_original_response("У игрока пока нет завершённых партий.")
            return
        row = rows[0]
        embed = disnake.Embed(title="🎭 Статистика мафии", description=user.mention, color=disnake.Color.dark_purple())
        for name, key in METRICS.items():
            embed.add_field(name="Процент побед" if key == "win_rate" else name, value=f"{row[key]}{'%' if key == 'win_rate' else ''}")
        await inter.edit_original_response(embed=embed)

    async def handle_component(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id or ''
        dm_confirmation = inter.guild_id is None and custom_id.startswith('mafia:confirm_start:')
        if not custom_id.startswith('mafia:') or (inter.guild_id != GUILD_ID and not dm_confirmation):
            return
        if not dm_confirmation and not isinstance(inter.author, disnake.Member):
            return
        # Components default to updating the source message. A separate deferred
        # response is required for private errors, roles and target menus.
        await inter.response.defer(with_message=True, ephemeral=True)
        try:
            game_id = int(custom_id.split(':')[2])
        except (ValueError, IndexError):
            await inter.edit_original_response('Некорректная кнопка.')
            return
        async with self.game_lock(game_id):
            await self._handle_component(inter)

    async def _handle_component(self, inter: disnake.MessageInteraction) -> None:
        custom_id = inter.component.custom_id or ""
        if not custom_id.startswith("mafia:"):
            return
        dm_confirmation = inter.guild_id is None and custom_id.startswith('mafia:confirm_start:')
        if inter.guild_id != GUILD_ID and not dm_confirmation:
            return
        try:
            parts = custom_id.split(":")
            action, game_id = parts[1], int(parts[2])
            member = inter.author
            if dm_confirmation:
                guild = self.bot.get_guild(GUILD_ID)
                if guild is None:
                    raise rules.GameError('Сервер игры недоступен. Попробуйте позже.')
                try:
                    member = await guild.fetch_member(inter.author.id)
                except disnake.NotFound:
                    raise rules.GameError('Для подтверждения нужно состоять на сервере игры.')
            if not isinstance(member, disnake.Member):
                return
            level = await member_access_level(self.bot, member)
            if member.bot:
                raise rules.GameError("Боты не могут играть.")
            if action in {'join', 'role', 'act', 'confirm_start'} and not await has_family_role(self.bot, member):
                raise rules.GameError("Участие доступно только с семейной ролью.")
            if action in {"start", "pause", "stop", "confirm_stop"} and level < 5:
                raise rules.GameError("Управлять игрой можно с 5 уровня допуска.")
            await self.advance_game(game_id)
            components = []
            async with self.bot.database.mafia_transaction(GUILD_ID, game_id) as game:
                if game is None:
                    raise rules.GameError("Игра не найдена.")
                state = game["state"]
                now = disnake.utils.utcnow().timestamp()
                if action == "join":
                    if await self.bot.database.has_other_mafia_role_hold(GUILD_ID, inter.author.id, game_id):
                        raise rules.GameError('Сначала дождитесь возврата админских ролей после прошлой партии.')
                    rules.join(state, inter.author.id, inter.author.display_name)
                    text = "Вы присоединились к игре."
                elif action == "leave":
                    rules.leave(state, inter.author.id)
                    text = "Вы вышли из лобби."
                elif action == "start":
                    await self.rooms.preflight_start(game)
                    rules.request_start(state)
                    text = 'Запуск объявлен. Ожидаем подтверждения всех игроков в ЛС или на панели игры.'
                elif action == 'confirm_start':
                    if len(parts) != 4:
                        raise rules.GameError('Некорректное подтверждение.')
                    ready = rules.confirm_start(state, member.id, int(parts[3]))
                    text = 'Участие подтверждено. Ожидаем остальных игроков.'
                    if ready:
                        await self.rooms.preflight_start(game)
                        rules.start(state, now)
                        state.update(setup_pending=True, paused=True, remaining=rules.DURATIONS['night'], deadline=None)
                        text = 'Все подтвердили участие. Бот подготавливает комнаты и роли; затем начнётся первая ночь.'
                elif action == "pause":
                    rules.toggle_pause(state, now)
                    text = "Игра на паузе." if state["paused"] else "Игра продолжена."
                elif action == "stop":
                    if state["phase"] not in rules.ACTIVE_PHASES:
                        raise rules.GameError("Игра уже завершена.")
                    text = "Отменить игру? Результаты этой партии не попадут в рейтинги."
                    components = [disnake.ui.Button(label="Подтвердить отмену", style=disnake.ButtonStyle.danger, custom_id=f"mafia:confirm_stop:{game_id}")]
                elif action == "confirm_stop":
                    rules.cancel(state)
                    text = "Игра отменена."
                elif action == "role":
                    text, components = private_panel(game, inter.author.id)
                elif action == "act":
                    if len(parts) != 4 or len(inter.values) != 1:
                        raise rules.GameError("Некорректное действие.")
                    text = rules.act(state, inter.author.id, inter.values[0], int(parts[3]), now)
                else:
                    raise rules.GameError("Неизвестное действие.")
            await inter.edit_original_response(content=text, components=components, allowed_mentions=disnake.AllowedMentions.none())
            await self.maintain_game(game_id)
            await self.sync_message(game_id)
        except rules.GameError as error:
            await inter.edit_original_response(str(error), components=[])
        except (ValueError, IndexError):
            await inter.edit_original_response("Некорректная или устаревшая кнопка.", components=[])
        except Exception:
            logging.exception("Could not handle mafia interaction")
            await inter.edit_original_response("Не удалось обработать действие. Откройте меню заново и проверьте состояние игры.", components=[])

    @commands.Cog.listener("on_button_click")
    async def on_mafia_button(self, inter: disnake.MessageInteraction) -> None:
        await self.handle_component(inter)

    @commands.Cog.listener("on_dropdown")
    async def on_mafia_select(self, inter: disnake.MessageInteraction) -> None:
        await self.handle_component(inter)

    @commands.Cog.listener()
    async def on_member_remove(self, member: disnake.Member) -> None:
        if member.guild.id != GUILD_ID:
            return
        try:
            game = await self.bot.database.get_mafia_game(GUILD_ID)
            if game is None:
                return
            async with self.game_lock(game['id']):
                async with self.bot.database.mafia_transaction(GUILD_ID, game["id"]) as current:
                    if current is not None:
                        rules.depart(current["state"], member.id)
                await self.maintain_game(game['id'])
                await self.sync_message(game["id"])
        except Exception:
            logging.exception("Could not remove departed member from mafia game")

    @commands.Cog.listener()
    async def on_member_update(self, before: disnake.Member, after: disnake.Member) -> None:
        if after.guild.id != GUILD_ID or {r.id for r in before.roles} == {r.id for r in after.roles}:
            return
        try:
            game = await self.bot.database.get_mafia_game(GUILD_ID)
            if game is None or str(after.id) not in game['state']['players']:
                return
            if not await has_family_role(self.bot, after):
                await self.on_member_remove(after)
            elif after.guild_permissions.administrator and game['state']['phase'] != 'lobby':
                async with self.game_lock(game['id']):
                    self._room_sync.pop(game['id'], None)
                    await self.maintain_game(game['id'])
        except Exception:
            logging.exception('Could not reconcile mafia participant roles')
