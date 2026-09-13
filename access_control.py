from __future__ import annotations

from typing import TYPE_CHECKING

import disnake

from config import DEVELOPER_USER_ID, FAMILY_MEMBER_ROLE_ID, FAMILY_RANK_ROLE_IDS, GUILD_ID, TARGET_ROLE_IDS
from cogs.family_ranks import family_rank_from_roles

if TYPE_CHECKING:
    from bot import InterpolovBot


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
    "mafia create": 5,
    "mafia status": 1,
    "mafia top": 1,
    "mafia stats": 1,
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
    "giveaway create": 4,
    "giveaway list": 4,
    "giveaway info": 4,
    "giveaway end": 5,
    "giveaway reroll": 5,
    "giveaway cancel": 5,
    "applications setup": 5,
    "applications block": 5,
    "applications unblock": 5,
    "applications reset_cooldown": 5,
    "blacklist add": 5,
    "access command": 5,
    "access role_add": 5,
    "access role_remove": 5,
    "access show": 5,
    "settings server_add": 5,
    "settings server_remove": 5,
    "settings show": 5,
    "broadcast": 5,
    "dm_notifications": 6,
}

# Эти команды нельзя ослабить пользовательской настройкой.
PROTECTED_COMMAND_LEVELS = {
    "applications roles": 5,
    "applications roles_show": 5,
    "mafia create": 5,
    "applications block": 5,
    "applications unblock": 5,
    "applications reset_cooldown": 5,
    "режим_проверки": 4,
    "blacklist add": 5,
    "giveaway end": 5,
    "giveaway reroll": 5,
    "giveaway cancel": 5,
    "access command": 5,
    "access role_add": 5,
    "access role_remove": 5,
    "access show": 5,
    "settings server_add": 5,
    "settings server_remove": 5,
    "settings show": 5,
    "broadcast": 5,
    "dm_notifications": 6,
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
    bot: "InterpolovBot", guild_id: int, command_name: str
) -> int:
    if command_name in PROTECTED_COMMAND_LEVELS:
        return PROTECTED_COMMAND_LEVELS[command_name]
    configured = await bot.database.get_command_access_level(guild_id, command_name)
    level = configured or DEFAULT_COMMAND_LEVELS.get(command_name, 5)
    return level


async def member_access_level(
    bot: "InterpolovBot", member: disnake.Member
) -> int:
    if member.id == DEVELOPER_USER_ID:
        return 6
    if member.guild_permissions.administrator:
        return 5

    role_ids = {role.id for role in member.roles}
    level = await bot.database.get_highest_role_access_level(
        member.guild.id, role_ids
    )
    level = max(level, await bot.database.get_mafia_saved_access(member.guild.id, member.id))

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
        family_roles = {
            row["role_id"] for row in await bot.database.get_families(member.guild.id)
            if row["role_id"] is not None
        }
        if role_ids.intersection(family_roles | {FAMILY_MEMBER_ROLE_ID}):
            level = max(level, 1)
        if role_ids.intersection(TARGET_ROLE_IDS):
            level = max(level, 1)

    return level


async def family_role_ids(bot: "InterpolovBot", guild_id: int) -> set[int]:
    if guild_id != GUILD_ID:
        return set()
    return {FAMILY_MEMBER_ROLE_ID, *TARGET_ROLE_IDS} | {
        row["role_id"] for row in await bot.database.get_families(guild_id)
        if row["role_id"] is not None
    }


async def application_staff_role_ids(
    bot: "InterpolovBot", guild_id: int, game_server: str
) -> set[int]:
    # Legacy applications with several servers stay visible only to admins and
    # the applicant: granting the union would disclose them across teams.
    return {
        row["role_id"]
        for row in await bot.database.get_application_staff_roles(guild_id)
        if row["server_name"] == game_server
    }


async def can_review_application(bot: "InterpolovBot", member: disnake.Member, application) -> bool:
    if member.guild.id != application["guild_id"]:
        return False
    if member.id == DEVELOPER_USER_ID or member.guild_permissions.administrator:
        return True
    return (
        await member_access_level(bot, member) >= 2
        and bool({role.id for role in member.roles} & await application_staff_role_ids(
            bot, member.guild.id, application["game_server"]
        ))
    )


async def has_family_role(bot: "InterpolovBot", member: disnake.Member) -> bool:
    return bool({role.id for role in member.roles} & await family_role_ids(bot, member.guild.id))


async def promotion_approval_error(
    bot: "InterpolovBot", reviewer: disnake.Member,
    applicant: disnake.Member, target_rank: str,
) -> str | None:
    if reviewer.guild.id != GUILD_ID or applicant.guild.id != GUILD_ID:
        return "Повышения доступны только на основном сервере."
    if reviewer.id == applicant.id:
        return "Нельзя одобрить собственную заявку."
    if await member_access_level(bot, reviewer) < 4:
        return "Рассматривать повышение можно с уровня допуска 4."
    if applicant.id == DEVELOPER_USER_ID:
        return "Автоматическое изменение ролей разработчика (уровень 6) запрещено."
    if target_rank not in FAMILY_RANK_ROLE_IDS:
        return "Неизвестный ранг."
    reviewer_rank = family_rank_from_roles({role.id for role in reviewer.roles})
    ranks = list(FAMILY_RANK_ROLE_IDS)
    if ranks.index(target_rank) >= ranks.index(reviewer_rank):
        return "Можно выдать только ранг строго ниже своего семейного ранга."
    target_role = reviewer.guild.get_role(FAMILY_RANK_ROLE_IDS[target_rank])
    if target_role is None:
        return "Discord-роль выбранного ранга не найдена."
    if target_role >= reviewer.top_role or applicant.top_role >= reviewer.top_role:
        return "Роль повышения и высшая роль заявителя должны быть строго ниже вашей Discord-роли."
    return None
