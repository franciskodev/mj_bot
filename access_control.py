from __future__ import annotations

from typing import TYPE_CHECKING

import disnake

from config import DEVELOPER_USER_ID, GUILD_ID, TARGET_ROLE_IDS

if TYPE_CHECKING:
    from bot import IntoleranceBot


ACCESS_LEVEL_NAMES = {
    0: "Нет доступа",
    1: "Член семьи",
    2: "Рекрутёр",
    3: "Глава отдела",
    4: "Старший состав",
    5: "Администратор",
    6: "Разработчик",
}

# Эти значения используются, пока администратор не задаст переопределение
# командой /access command. Неизвестная новая команда безопасно получает уровень 5.
DEFAULT_COMMAND_LEVELS = {
    "help": 1,
    "ping": 1,
    "myinfo": 1,
    "leave request": 1,
    "управление": 4,
    "department add": 3,
    "department remove": 3,
    "department setup": 3,
    "meeting check": 4,
    "режим_проверки": 4,
    "user add": 4,
    "user edit": 4,
    "user view": 4,
    "user remove": 4,
    "warning issue": 4,
    "warning list": 4,
    "warning revoke": 4,
    "applications setup": 5,
    "blacklist add": 5,
    "access command": 5,
    "access role_add": 5,
    "access role_remove": 5,
    "access show": 5,
    "broadcast": 5,
}

# Эти команды нельзя ослабить пользовательской настройкой.
PROTECTED_COMMAND_LEVELS = {
    "blacklist add": 5,
    "access command": 5,
    "access role_add": 5,
    "access role_remove": 5,
    "access show": 5,
    "broadcast": 5,
}


def interaction_command_name(inter: disnake.ApplicationCommandInteraction) -> str:
    parts = [inter.data.name]
    options = list(inter.data.options or [])
    while options and options[0].type in {
        disnake.OptionType.sub_command,
        disnake.OptionType.sub_command_group,
    }:
        option = options[0]
        parts.append(option.name)
        options = list(option.options or [])
    return " ".join(parts)


async def command_access_level(
    bot: "IntoleranceBot", guild_id: int, command_name: str
) -> int:
    if command_name in PROTECTED_COMMAND_LEVELS:
        return PROTECTED_COMMAND_LEVELS[command_name]
    configured = await bot.database.get_command_access_level(guild_id, command_name)
    level = configured or DEFAULT_COMMAND_LEVELS.get(command_name, 5)
    return level


async def member_access_level(
    bot: "IntoleranceBot", member: disnake.Member
) -> int:
    if member.id == DEVELOPER_USER_ID:
        return 6
    if member.guild_permissions.administrator:
        return 5

    role_ids = {role.id for role in member.roles}
    level = await bot.database.get_highest_role_access_level(
        member.guild.id, role_ids
    )

    departments = await bot.database.get_department_configs(member.guild.id)
    for department in departments:
        if department["head_role_id"] in role_ids:
            level = max(level, 3)
        elif (
            department["department"] == "recruiters"
            and department["member_role_id"] in role_ids
        ):
            level = max(level, 2)

    if member.guild.id == GUILD_ID:
        application_config = await bot.database.get_application_config(member.guild.id)
        if application_config is not None:
            if role_ids.intersection(application_config["reviewer_role_ids"]):
                level = max(level, 2)
            family_roles = {
                application_config["interpolov_role_id"],
                application_config["intolerance_role_id"],
            }
            if role_ids.intersection(family_roles):
                level = max(level, 1)
        if role_ids.intersection(TARGET_ROLE_IDS):
            level = max(level, 1)

    return level
