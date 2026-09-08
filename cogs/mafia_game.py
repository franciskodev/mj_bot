"""Mafia rules. State contains secrets; only explicit public fields go to Discord."""

from collections import Counter
import secrets

from config import (
    MAFIA_MIN_PLAYERS, MAFIA_MAX_PLAYERS, MAFIA_NIGHT_SECONDS,
    MAFIA_DAY_SECONDS, MAFIA_VOTE_SECONDS, MAFIA_MAX_ROUNDS,
)

ROLES = {"civilian": "Мирный", "mafia": "Мафия", "doctor": "Доктор", "commissioner": "Комиссар"}
PHASES = {"lobby": "Набор игроков", "night": "Ночь", "day": "Обсуждение", "vote": "Голосование", "finished": "Игра завершена", "cancelled": "Игра отменена"}
DURATIONS = {"night": MAFIA_NIGHT_SECONDS, "day": MAFIA_DAY_SECONDS, "vote": MAFIA_VOTE_SECONDS}
ACTIVE_PHASES = {"lobby", "night", "day", "vote"}


class GameError(ValueError):
    pass


def new_game(host_id: int, voice_id: int | None = None) -> dict:
    return {
        "phase": "lobby", "host_id": host_id, "voice_id": voice_id,
        "players": {}, "round": 0, "token": 0, "deadline": None,
        "paused": False, "remaining": None, "actions": {}, "votes": {},
        "winner": None, "event": "Набор открыт. Для старта нужно 6–12 игроков.",
    }


def join(state: dict, user_id: int, name: str) -> None:
    if state["phase"] != "lobby":
        raise GameError("Набор уже закрыт.")
    key = str(user_id)
    if key in state["players"]:
        raise GameError("Вы уже участвуете.")
    if len(state["players"]) >= MAFIA_MAX_PLAYERS:
        raise GameError("В лобби уже 12 игроков.")
    state["players"][key] = {"name": name, "role": None, "alive": True, "checks": {}}
    state["event"] = f"<@{user_id}> присоединился к игре."


def leave(state: dict, user_id: int) -> None:
    if state["phase"] != "lobby":
        raise GameError("Покинуть лобби можно только до начала игры.")
    if state["players"].pop(str(user_id), None) is None:
        raise GameError("Вы не участвуете в этой игре.")
    state["event"] = f"<@{user_id}> вышел из лобби."


def enter_phase(state: dict, phase: str, now: float) -> None:
    state["phase"] = phase
    state["token"] += 1
    state["actions"] = {}
    state["votes"] = {}
    state["deadline"] = now + DURATIONS[phase]


def start(state: dict, now: float) -> None:
    if state["phase"] != "lobby":
        raise GameError("Игра уже началась.")
    count = len(state["players"])
    if not MAFIA_MIN_PLAYERS <= count <= MAFIA_MAX_PLAYERS:
        raise GameError("Для старта нужно от 6 до 12 игроков.")
    mafia_count = 2 if count <= 8 else 3
    roles = ["mafia"] * mafia_count + ["doctor", "commissioner"] + ["civilian"] * (count - mafia_count - 2)
    secrets.SystemRandom().shuffle(roles)
    for player, role in zip(state["players"].values(), roles):
        player["role"] = role
    state["round"] = 1
    state["event"] = "Роли розданы. Нажмите «Моя роль / действие», чтобы узнать свою роль."
    enter_phase(state, "night", now)


def finish(state: dict, winner: str) -> None:
    state.update(phase="finished", winner=winner, deadline=None, paused=False, remaining=None,
                 setup_pending=False, room_error=False)
    state["token"] += 1


def check_winner(state: dict) -> bool:
    alive = [p for p in state["players"].values() if p["alive"]]
    mafia = sum(p["role"] == "mafia" for p in alive)
    if not mafia:
        finish(state, "town")
    elif mafia >= len(alive) - mafia:
        finish(state, "mafia")
    else:
        return False
    return True


def unique_choice(choices) -> str | None:
    counts = Counter(choices)
    if not counts:
        return None
    leaders = [key for key, count in counts.items() if count == max(counts.values())]
    return leaders[0] if len(leaders) == 1 else None


def ensure_turn(state: dict, user_id: int, token: int, now: float) -> dict:
    if state["phase"] not in {"night", "vote"} or token != state["token"]:
        raise GameError("Это меню устарело. Откройте действие заново.")
    if state["paused"]:
        raise GameError("Игра на паузе.")
    if now >= state["deadline"]:
        raise GameError("Время хода истекло.")
    player = state["players"].get(str(user_id))
    if player is None or not player["alive"]:
        raise GameError("Действие доступно только живым участникам.")
    return player


def act(state: dict, user_id: int, target: str, token: int, now: float) -> str:
    player = ensure_turn(state, user_id, token, now)
    key = str(user_id)
    if state["phase"] == "vote" and target == "skip":
        state["votes"][key] = target
        return "Вы воздержались. До конца голосования выбор можно изменить."
    victim = state["players"].get(target)
    if victim is None or not victim["alive"]:
        raise GameError("Выберите живого участника.")
    if state["phase"] == "vote":
        if target == key:
            raise GameError("Нельзя голосовать против себя.")
        state["votes"][key] = target
        return "Голос сохранён. До конца голосования выбор можно изменить."
    role = player["role"]
    if role == "civilian":
        raise GameError("У мирного жителя нет ночного действия.")
    if role == "mafia" and victim["role"] == "mafia":
        raise GameError("Нельзя выбрать участника мафии.")
    if role == "commissioner":
        if target == key:
            raise GameError("Нельзя проверить себя.")
        existing = state["actions"].get(key)
        if existing is not None and existing != target:
            raise GameError("Можно проверить только одного игрока за ночь.")
        result = "Мафия" if victim["role"] == "mafia" else "Не мафия"
        player["checks"][target] = result
        state["actions"][key] = target
        return f"Проверка <@{target}>: **{result}**."
    state["actions"][key] = target
    return "Ночное действие сохранено. До конца ночи выбор можно изменить."


def advance(state: dict, now: float) -> bool:
    if state["paused"] or state["deadline"] is None or now < state["deadline"]:
        return False
    if state["phase"] == "night":
        mafia_choices = [target for uid, target in state["actions"].items()
                         if state["players"][uid]["role"] == "mafia" and state["players"][uid]["alive"]]
        victim = unique_choice(mafia_choices)
        protected = {target for uid, target in state["actions"].items()
                     if state["players"][uid]["role"] == "doctor" and state["players"][uid]["alive"]}
        if victim is not None and victim not in protected:
            state["players"][victim]["alive"] = False
            state["event"] = f"Этой ночью погиб <@{victim}>. Роль будет раскрыта в конце игры."
        else:
            state["event"] = "Этой ночью никто не погиб."
        if not check_winner(state):
            enter_phase(state, "day", now)
    elif state["phase"] == "day":
        state["event"] = "Обсуждение завершено. Выберите игрока или воздержитесь."
        enter_phase(state, "vote", now)
    elif state["phase"] == "vote":
        victim = unique_choice(state["votes"].values())
        if victim is not None and victim != "skip":
            state["players"][victim]["alive"] = False
            state["event"] = f"По результатам голосования выбыл <@{victim}>."
        else:
            state["event"] = "Голосование завершилось без исключения игрока."
        if not check_winner(state):
            if state["round"] >= MAFIA_MAX_ROUNDS:
                state["event"] += " Достигнут лимит раундов — ничья."
                finish(state, "draw")
            else:
                state["round"] += 1
                enter_phase(state, "night", now)
    else:
        return False
    return True


def toggle_pause(state: dict, now: float) -> None:
    if state.get("setup_pending"):
        raise GameError("Подготовка каналов и ролей ещё не закончена. Можно отменить игру.")
    if state["phase"] not in DURATIONS:
        raise GameError("Пауза доступна только во время игры.")
    if state["paused"]:
        state["deadline"] = now + state["remaining"]
        state["remaining"] = None
        state["paused"] = False
    else:
        state["remaining"] = max(0, state["deadline"] - now)
        state["deadline"] = None
        state["paused"] = True
    state["token"] += 1


def cancel(state: dict) -> None:
    if state["phase"] not in ACTIVE_PHASES:
        raise GameError("Игра уже завершена.")
    state.update(phase="cancelled", deadline=None, paused=False, remaining=None,
                 setup_pending=False, room_error=False,
                 event="Игра отменена. Результат не учитывается в статистике.")
    state["token"] += 1


def depart(state: dict, user_id: int) -> None:
    """A server departure removes a lobby seat or eliminates an active player."""
    key = str(user_id)
    player = state["players"].get(key)
    if player is None or state["phase"] not in ACTIVE_PHASES:
        return
    if state["phase"] == "lobby":
        del state["players"][key]
        state["event"] = f"<@{user_id}> исключён из лобби: недоступен или потерял семейную роль."
        return
    if not player["alive"]:
        return
    player["alive"] = False
    for field in ("actions", "votes"):
        state[field] = {uid: target for uid, target in state[field].items()
                        if uid != key and target != key}
    state["event"] = f"<@{key}> выбыл: покинул сервер или потерял семейную роль."
    check_winner(state)
