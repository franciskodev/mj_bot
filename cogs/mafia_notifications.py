"""Personal Mafia notifications, persisted together with the game change."""
import json
import logging

import disnake

from config import GUILD_ID
from cogs import mafia_game as rules


ROLE_ACTIONS = {
    'mafia': 'Ночью выберите жертву через «Моя роль / действие».',
    'doctor': 'Ночью выберите, кого защитить, через «Моя роль / действие».',
    'commissioner': 'Ночью проверьте одного игрока через «Моя роль / действие».',
    'civilian': 'Ночью действий нет. Днём обсуждайте подозрения и голосуйте.',
}


def personal_events(before: dict, state: dict) -> list[dict]:
    """Build only the recipient's own secrets; never copy a whole game state."""
    events = []

    def add(uid, kind, text, **extra):
        events.append(dict(user_id=int(uid), kind=kind, text=text, **extra))

    previous = before['players']
    players = state['players']
    for uid in players.keys() - previous.keys():
        add(uid, 'join', 'Вы присоединились к игре. После объявления старта подтвердите участие.')
    for uid in previous.keys() - players.keys():
        add(uid, 'leave', 'Вы вышли из лобби или были исключены из него.')
    requesting = state.get('confirmation_pending') and (
        not before.get('confirmation_pending') or before['token'] != state['token']
    )
    confirmed = set(state.get('confirmations', [])) - set(before.get('confirmations', []))
    for uid, player in players.items():
        old = previous.get(uid, {})
        if requesting:
            add(uid, 'confirm', 'Организатор объявил запуск игры. Подтвердите участие кнопкой ниже. '
                'Первая ночь начнётся после подтверждения всех игроков.', token=state['token'])
        if uid in confirmed:
            add(uid, 'confirmed', 'Вы подтвердили участие в игре.')
        if before.get('confirmation_pending') and not state.get('confirmation_pending') and state['phase'] == 'lobby':
            add(uid, 'confirmation_reset', 'Состав лобби изменился. Подтверждения сброшены; ожидаем нового запуска.')
        if player['role'] and not state.get('setup_pending') and state['phase'] in rules.DURATIONS:
            if old.get('role') != player['role'] or before.get('setup_pending'):
                add(uid, 'role', f"Ваша роль: **{rules.ROLES[player['role']]}**.\n{ROLE_ACTIONS[player['role']]}")
        if old.get('alive') and not player['alive']:
            add(uid, 'eliminated', 'Вы выбыли из игры. Голосование и ночные действия больше недоступны.')
        for field in ('actions', 'votes'):
            target = state[field].get(uid)
            if target is None or before[field].get(uid) == target:
                continue
            choice = 'воздержаться' if target == 'skip' else f'<@{target}>'
            if field == 'votes':
                text = f'Ваш голос: {choice}. До конца голосования выбор можно изменить.'
            elif player['role'] == 'commissioner':
                text = f"Ваша проверка <@{target}>: **{player['checks'][target]}**. Цель этой ночью изменить нельзя."
            else:
                action = 'Защита' if player['role'] == 'doctor' else 'Выбор жертвы'
                text = f'{action}: {choice}. До конца ночи выбор можно изменить.'
            add(uid, field, text)
        changed_phase = before['phase'] != state['phase'] or before['round'] != state['round']
        prepared = before.get('setup_pending') and not state.get('setup_pending')
        changed_pause = before['paused'] != state['paused']
        if not (changed_phase or prepared or changed_pause) or state.get('setup_pending'):
            continue
        if state['phase'] in {'finished', 'cancelled'}:
            text = 'Игра отменена. Результат не учитывается в статистике.'
            if state['phase'] == 'finished':
                text = {'town': 'Игра завершена. Победил город!', 'mafia': 'Игра завершена. Победила мафия!',
                        'draw': 'Игра завершена вничью.'}[state['winner']]
            add(uid, 'result', text)
            continue
        text = f"{rules.PHASES[state['phase']]} · Раунд {state['round']}."
        if changed_pause and not state['paused'] and not prepared:
            text += ' Игра продолжена.'
        if state['paused']:
            text += ' Игра на паузе; действия недоступны.'
        elif not player['alive']:
            text += ' Вы выбыли и можете наблюдать за игрой.'
        elif state['phase'] == 'night':
            text += ' ' + ROLE_ACTIONS[player['role']]
        elif state['phase'] == 'vote':
            text += ' Выберите игрока или воздержитесь через «Моя роль / действие».'
        elif state['phase'] == 'day':
            text += ' Обсуждайте подозрения в каналах обсуждения.'
        if state.get('deadline') is not None:
            text += f"\nКонец фазы: <t:{int(state['deadline'])}:R>."
        add(uid, 'phase', text)
    return events


async def deliver_notifications(bot) -> None:
    rows = await bot.database.get_mafia_notifications(GUILD_ID)
    if not rows:
        return
    enabled = await bot.database.get_dm_notifications_enabled()
    for row in rows:
        payload = json.loads(row['payload'])
        game = await bot.database.get_mafia_game(GUILD_ID, row['game_id'])
        if not enabled or game is None:
            await bot.database.finish_mafia_notification(row['id'])
            continue
        components = []
        if payload['kind'] == 'confirm':
            state = game['state']
            if (not state.get('confirmation_pending') or state['token'] != payload['token']
                    or str(row['discord_id']) not in state['players']
                    or str(row['discord_id']) in state.get('confirmations', [])):
                await bot.database.finish_mafia_notification(row['id'])
                continue
            components.append(disnake.ui.Button(
                label='Подтверждаю участие', style=disnake.ButtonStyle.success,
                custom_id=f"mafia:confirm_start:{game['id']}:{payload['token']}",
            ))
        if game.get('message_id') and not game['state'].get('rooms_deleted'):
            components.append(disnake.ui.Button(
                label='Открыть игру', style=disnake.ButtonStyle.link,
                url=f"https://discord.com/channels/{GUILD_ID}/{game['channel_id']}/{game['message_id']}",
            ))
        try:
            user = bot.get_user(row['discord_id']) or await bot.fetch_user(row['discord_id'])
            message = await user.send(
                content=f"🎭 **Мафия #{game['id']}**\n{payload['text']}", components=components,
                allowed_mentions=disnake.AllowedMentions.none(),
            )
        except (disnake.Forbidden, disnake.NotFound):
            # Closed DMs never block readiness: the same button is on the guild panel.
            await bot.database.finish_mafia_notification(row['id'])
        except disnake.DiscordException:
            logging.warning('Could not deliver Mafia notification %s to %s', row['id'], row['discord_id'])
            await bot.database.retry_mafia_notification(row['id'])
        else:
            await bot.database.finish_mafia_notification(row['id'], message.id)
