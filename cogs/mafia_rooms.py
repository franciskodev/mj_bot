"""Discord rooms and durable, reversible administrator-role suspension for Mafia."""
import json
import logging
import secrets

import disnake

from access_control import family_role_ids, member_access_level
from config import DEVELOPER_USER_ID, GUILD_ID
from cogs import mafia_game as rules


ROOM_NAMES = {
    'events': 'события', 'discussion': 'обсуждение', 'mafia_text': 'чат-мафии',
    'discussion_voice': 'Обсуждение', 'dead_voice': 'Выбывшие', 'mafia_voice': 'Мафия',
}
VOICE_ROOMS = {'discussion_voice', 'dead_voice', 'mafia_voice'}


def admin_roles_to_suspend(member: disnake.Member, family_ids: set[int]) -> list[disnake.Role]:
    roles = [role for role in member.roles if role.permissions.administrator]
    if member.id == member.guild.owner_id or member.guild.default_role.permissions.administrator:
        raise rules.GameError(f'{member.mention}: права владельца или @everyone нельзя временно снять. Этот игрок должен выйти из лобби.')
    if not roles:
        return []
    me = member.guild.me
    if me is None or not me.guild_permissions.manage_roles or member.top_role >= me.top_role:
        raise rules.GameError(f'{member.mention}: для снятия админских ролей роль бота должна быть выше ролей игрока.')
    if any(role.managed or role.is_default() or role >= me.top_role for role in roles):
        raise rules.GameError(f'{member.mention}: одну из админских ролей бот не может снять. Уберите её вручную перед стартом.')
    if any(role.id in family_ids for role in roles):
        raise rules.GameError(f'{member.mention}: семейная роль одновременно даёт Administrator. Разделите семейную и админскую роли перед игрой.')
    return roles


def room_overwrites(guild, state: dict, kind: str, family_ids: set[int]) -> dict:
    # Explicit member overwrites beat ordinary role grants. Administrator itself
    # bypasses these, so it must be suspended separately before roles are revealed.
    deny = disnake.PermissionOverwrite(
        view_channel=False, send_messages=False, connect=False, speak=False,
        manage_channels=False, manage_roles=False, manage_messages=False,
        manage_webhooks=False, create_public_threads=False, create_private_threads=False,
        send_messages_in_threads=False, add_reactions=False, mention_everyone=False,
        move_members=False, mute_members=False, deafen_members=False,
    )
    overwrites = {guild.default_role: deny}
    if guild.me is not None:
        overwrites[guild.me] = disnake.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            connect=True, speak=True, manage_channels=True, manage_roles=True,
            embed_links=True, attach_files=True, move_members=True,
        )
    public = kind in {'category', 'events', 'discussion'}
    ended = state['phase'] in {'finished', 'cancelled'}
    if public:
        for role_id in family_ids:
            role = guild.get_role(role_id)
            if role is not None:
                overwrites[role] = disnake.PermissionOverwrite(view_channel=True, read_message_history=True)
        host = guild.get_member(state['host_id'])
        if host is not None:
            overwrites[host] = disnake.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False)
    for uid, player in state['players'].items():
        member = guild.get_member(int(uid))
        if member is None:
            continue
        alive = player['alive']
        night = state['phase'] == 'night' and not state['paused']
        day = state['phase'] in {'lobby', 'day', 'vote'} and not state['paused']
        view = public
        connect = speak = send = False
        if kind == 'discussion':
            send = alive and day and not ended
        elif kind == 'discussion_voice':
            view = True
            connect = alive and not ended
            speak = connect and day
        elif kind == 'dead_voice':
            view = not alive or ended
            connect = not alive and not ended
            speak = connect
        elif kind in {'mafia_text', 'mafia_voice'}:
            view = ended or (alive and player['role'] == 'mafia' and not state.get('setup_pending'))
            send = view and night and not ended
            connect = view and night and not ended
            speak = connect
        overwrites[member] = disnake.PermissionOverwrite(
            view_channel=view, read_message_history=view, send_messages=send,
            connect=connect, speak=speak, stream=speak,
            manage_channels=False, manage_roles=False, manage_messages=False,
            manage_webhooks=False, move_members=False, mute_members=False,
            deafen_members=False, add_reactions=False, send_messages_in_threads=False,
            create_public_threads=False, create_private_threads=False,
        )
    return overwrites


class MafiaRooms:
    def __init__(self, bot):
        self.bot = bot

    async def patch_state(self, game_id: int, **values) -> dict:
        async with self.bot.database.mafia_transaction(GUILD_ID, game_id) as game:
            if game is None:
                raise rules.GameError('Игра не найдена.')
            game['state'].update(values)
        return await self.bot.database.get_mafia_game(GUILD_ID, game_id)

    async def remember_room(self, game_id: int, kind: str, channel_id: int) -> dict:
        async with self.bot.database.mafia_transaction(GUILD_ID, game_id) as game:
            if game is None:
                raise rules.GameError('Игра не найдена.')
            game['state'].setdefault('rooms', {})[kind] = channel_id
            if kind == 'discussion_voice':
                game['state']['voice_id'] = channel_id
        return await self.bot.database.get_mafia_game(GUILD_ID, game_id)

    async def ensure_rooms(self, game: dict) -> dict:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None or guild.me is None:
            raise rules.GameError('Сервер недоступен.')
        if not guild.me.guild_permissions.manage_channels or not guild.me.guild_permissions.manage_roles:
            raise rules.GameError('Для создания комнат боту нужны права «Управлять каналами» и «Управлять ролями».')
        game_id = game['id']
        state = game['state']
        if not state.get('room_key'):
            game = await self.patch_state(game_id, room_key=secrets.token_hex(4), rooms_enabled=True)
            state = game['state']
        families = await family_role_ids(self.bot, GUILD_ID)
        category_id = state.get('rooms', {}).get('category')
        category = guild.get_channel(category_id) if category_id else None
        if category is None:
            # A random persisted suffix also identifies a category created just
            # before a crash between the Discord request and the database commit.
            name = f"Мафия #{game_id} · {state['room_key']}"
            channels = await guild.fetch_channels()
            category = next((c for c in channels if isinstance(c, disnake.CategoryChannel) and c.name == name), None)
            if category is None:
                category = await guild.create_category(name, overwrites=room_overwrites(guild, state, 'category', families), reason=f'Мафия #{game_id}')
            game = await self.remember_room(game_id, 'category', category.id)
        if not isinstance(category, disnake.CategoryChannel):
            raise rules.GameError('Категория игры недоступна.')
        # Fetch is necessary after a restart and after creation before cache events arrive.
        channels = await guild.fetch_channels()
        for kind, name in ROOM_NAMES.items():
            state = game['state']
            channel_id = state.get('rooms', {}).get(kind)
            channel = next((c for c in channels if c.id == channel_id and c.category_id == category.id), None)
            if channel is None:
                channel = next((c for c in channels if getattr(c, 'category_id', None) == category.id and c.name == name), None)
            expected = disnake.VoiceChannel if kind in VOICE_ROOMS else disnake.TextChannel
            if channel is not None and not isinstance(channel, expected):
                raise rules.GameError(f'Неверный тип игрового канала «{name}».')
            if channel is None:
                creator = category.create_voice_channel if kind in VOICE_ROOMS else category.create_text_channel
                channel = await creator(name, overwrites=room_overwrites(guild, state, kind, families), reason=f'Мафия #{game_id}')
                channels.append(channel)
            if channel_id != channel.id:
                game = await self.remember_room(game_id, kind, channel.id)
        return game

    async def preflight_start(self, game: dict) -> None:
        if game['state']['phase'] != 'lobby':
            raise rules.GameError('Игра уже началась.')
        if not 6 <= len(game['state']['players']) <= 12:
            raise rules.GameError('Для старта нужно от 6 до 12 игроков.')
        guild = self.bot.get_guild(GUILD_ID)
        families = await family_role_ids(self.bot, GUILD_ID)
        for uid in game['state']['players']:
            try:
                member = await guild.fetch_member(int(uid))
            except disnake.NotFound:
                raise rules.GameError(f'<@{uid}> покинул сервер. Обновите лобби.')
            if member.bot or not {r.id for r in member.roles} & families:
                raise rules.GameError(f'<@{uid}> не имеет семейной роли. Игрок должен выйти из лобби.')
            admin_roles_to_suspend(member, families)

    async def reconcile_players(self, game: dict) -> dict:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            raise rules.GameError('Сервер недоступен.')
        families = await family_role_ids(self.bot, GUILD_ID)
        removed = []
        for uid in game['state']['players']:
            member = guild.get_member(int(uid))
            if member is None:
                try:
                    member = await guild.fetch_member(int(uid))
                except disnake.NotFound:
                    removed.append(int(uid))
                    continue
            if member.bot or not {r.id for r in member.roles} & families:
                removed.append(int(uid))
        if removed:
            async with self.bot.database.mafia_transaction(GUILD_ID, game['id']) as current:
                for uid in removed:
                    rules.depart(current['state'], uid)
            game = await self.bot.database.get_mafia_game(GUILD_ID, game['id'])
        return game

    async def suspend_admin_roles(self, game: dict) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        families = await family_role_ids(self.bot, GUILD_ID)
        for uid in game['state']['players']:
            try:
                member = await guild.fetch_member(int(uid))
            except disnake.NotFound:
                continue
            roles = admin_roles_to_suspend(member, families)
            if not roles:
                continue
            level = await member_access_level(self.bot, member)
            # Commit BEFORE changing Discord. Even partial removals or a crash
            # leave a durable restoration record and the original command access.
            await self.bot.database.hold_mafia_roles(game['id'], GUILD_ID, member.id, [r.id for r in roles], level)
            await member.remove_roles(*roles, reason=f"Мафия #{game['id']}: временно, с сохранением допуска", atomic=True)
            refreshed = await guild.fetch_member(member.id)
            if refreshed.guild_permissions.administrator:
                raise rules.GameError(f'{member.mention}: не удалось отключить Administrator. Игра остаётся на паузе.')

    async def restore_admin_roles(self, game_id: int) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None or guild.me is None:
            return
        for hold in await self.bot.database.get_mafia_role_holds(game_id):
            try:
                member = await guild.fetch_member(hold['discord_id'])
                roles = [guild.get_role(role_id) for role_id in hold['role_ids']]
                missing = [r for r in roles if r is not None and r not in member.roles]
                if missing:
                    if not guild.me.guild_permissions.manage_roles or member.top_role >= guild.me.top_role:
                        continue
                    if any(r.managed or r >= guild.me.top_role for r in missing):
                        continue
                    await member.add_roles(*missing, reason=f'Мафия #{game_id}: возврат временно снятых ролей', atomic=True)
                    member = await guild.fetch_member(member.id)
                    if not {r.id for r in missing} <= {r.id for r in member.roles}:
                        continue
                # Deleted roles cannot be recreated; all existing roles must be restored.
                await self.bot.database.finish_mafia_role_hold(game_id, member.id)
            except disnake.NotFound:
                # A departed player is retried when they return to the guild.
                continue
            except disnake.DiscordException:
                logging.exception('Could not restore mafia roles for %s in game %s', hold['discord_id'], game_id)

    async def sync_permissions(self, game: dict) -> None:
        guild = self.bot.get_guild(GUILD_ID)
        state = game['state']
        families = await family_role_ids(self.bot, GUILD_ID)
        channels = {c.id: c for c in await guild.fetch_channels()}
        for kind, channel_id in state.get('rooms', {}).items():
            channel = channels.get(channel_id)
            if channel is None:
                raise rules.GameError('Канал игры удалён. Бот попробует восстановить его.')
            wanted = room_overwrites(guild, state, kind, families)
            if channel.overwrites != wanted:
                await channel.edit(overwrites=wanted, reason=f"Мафия #{game['id']}: {rules.PHASES[state['phase']]}")
        dead = channels.get(state.get('rooms', {}).get('dead_voice'))
        voices = {value for key, value in state.get('rooms', {}).items() if key in VOICE_ROOMS}
        if dead is not None and guild.me.guild_permissions.move_members and state['phase'] in rules.ACTIVE_PHASES:
            for uid, player in state['players'].items():
                member = guild.get_member(int(uid))
                if not player['alive'] and member and member.voice and member.voice.channel and member.voice.channel.id in voices and member.voice.channel.id != dead.id:
                    if member.id != DEVELOPER_USER_ID and member.top_role < guild.me.top_role:
                        await member.move_to(dead, reason=f"Мафия #{game['id']}: игрок выбыл")

    async def send_events(self, game: dict) -> None:
        events = await self.bot.database.get_mafia_events(game['id'])
        if not events:
            return
        channel_id = game['state'].get('rooms', {}).get('events', game['channel_id'])
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, disnake.TextChannel) or channel.guild.id != GUILD_ID:
            return
        sent = {}
        async for message in channel.history(limit=100):
            if self.bot.user and message.author.id == self.bot.user.id and message.embeds:
                sent[message.embeds[0].footer.text] = message.id
        for row in events:
            marker = f"Мафия #{game['id']} · событие {row['id']}"
            message_id = sent.get(marker)
            if message_id is None:
                event = json.loads(row['payload'])
                text = event['event']
                if event['paused']:
                    text += '\n⏸️ Игра на паузе.'
                if event['winner']:
                    text += '\n**' + {'town': 'Победил город!', 'mafia': 'Победила мафия!', 'draw': 'Ничья!'}[event['winner']] + '**'
                embed = disnake.Embed(title=f"{rules.PHASES[event['phase']]} · Раунд {event['round']}", description=text, color=disnake.Color.dark_purple())
                embed.set_footer(text=marker)
                message = await channel.send(embed=embed, allowed_mentions=disnake.AllowedMentions.none())
                message_id = message.id
            await self.bot.database.mark_mafia_event_sent(row['id'], message_id)
