"""Log role actions from Discord audit entries, including this bot's actions."""
from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands

from config import GUILD_ID, ROLE_LOG_CHANNEL_ID
from log_utils import send_admin_log

if TYPE_CHECKING:
    from bot import InterpolovBot


ROLE_ACTIONS = {
    disnake.AuditLogAction.role_create: "Роль создана",
    disnake.AuditLogAction.role_update: "Роль изменена",
    disnake.AuditLogAction.role_delete: "Роль удалена",
    disnake.AuditLogAction.member_role_update: "Роли участника изменены",
}
OVERWRITE_ACTIONS = {
    disnake.AuditLogAction.overwrite_create: "Права роли в канале добавлены",
    disnake.AuditLogAction.overwrite_update: "Права роли в канале изменены",
    disnake.AuditLogAction.overwrite_delete: "Права роли в канале удалены",
}
FIELD_NAMES = {
    "name": "Название",
    "permissions": "Права",
    "colour": "Цвет",
    "primary_colour": "Основной цвет",
    "secondary_colour": "Второй цвет",
    "tertiary_colour": "Третий цвет",
    "hoist": "Отображать отдельно",
    "mentionable": "Можно упоминать",
    "position": "Позиция",
    "icon": "Значок",
    "emoji": "Эмодзи",
    "managed": "Управляется интеграцией",
    "allow": "Разрешённые права",
    "deny": "Запрещённые права",
    "id": "ID",
    "type": "Тип",
    "flags": "Флаги",
}


def format_value(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, disnake.Permissions):
        names = ", ".join(name for name, enabled in value if enabled) or "нет"
        return f"{value.value} ({names})"
    return disnake.utils.escape_markdown(str(value))


def reference(value: object, *, role: bool = False, name: str | None = None) -> str:
    object_id = getattr(value, "id", None)
    if object_id is None:
        return "Неизвестно"
    label = name or getattr(value, "name", None)
    suffix = f" — {disnake.utils.escape_markdown(label)}" if label else ""
    return f"<@{'&' if role else ''}{object_id}> (`{object_id}`){suffix}"


class RoleLogsCog(commands.Cog):
    def __init__(self, bot: "InterpolovBot") -> None:
        self.bot = bot

    @commands.Cog.listener("on_audit_log_entry_create")
    async def log_role_action(self, entry: disnake.AuditLogEntry) -> None:
        if entry.guild.id != GUILD_ID:
            return
        title = ROLE_ACTIONS.get(entry.action) or OVERWRITE_ACTIONS.get(entry.action)
        if title is None:
            return

        before, after = entry.before, entry.after
        if entry.action in OVERWRITE_ACTIONS:
            # Gateway audit targets may be uncached Objects. Disnake preserves
            # the name on role overwrite targets, unlike member targets.
            overwrite_type = getattr(after, "type", getattr(before, "type", None))
            is_role = isinstance(entry.extra, disnake.Role) or (
                isinstance(entry.extra, disnake.Object) and hasattr(entry.extra, "name")
            )
            if overwrite_type != 0 and not is_role:
                return

        lines = [
            f"Исполнитель: {reference(entry.user)}",
            f"Время: <t:{int(entry.created_at.timestamp())}:F>",
            f"Причина: {format_value(entry.reason)}",
            f"Запись аудита: `{entry.id}`",
        ]
        if entry.action == disnake.AuditLogAction.member_role_update:
            lines.append(f"Участник: {reference(entry.target)}")
            for label, diff in (("Выданы роли", after), ("Сняты роли", before)):
                roles = getattr(diff, "roles", ())
                if roles:
                    lines.append(f"**{label}:**")
                    lines.extend(reference(role, role=True) for role in roles)
        else:
            if entry.action in OVERWRITE_ACTIONS:
                lines.append(f"Роль: {reference(entry.extra, role=True)}")
                lines.append(f"Канал: <#{entry.target.id}> (`{entry.target.id}`)")
            else:
                name = getattr(after, "name", None) or getattr(before, "name", None)
                lines.append(f"Роль: {reference(entry.target, role=True, name=name)}")
            old, new = dict(before), dict(after)
            for key in dict.fromkeys((*old, *new)):
                label = FIELD_NAMES.get(key, key)
                lines.append(
                    f"**{label}:** {format_value(old.get(key))} → {format_value(new.get(key))}"
                )

        # Large multi-role changes and permission lists must not exceed the
        # embed description limit. Keep every detail across consecutive pages.
        description = "\n".join(lines)
        pages = []
        while description:
            end = len(description) if len(description) <= 4000 else description.rfind("\n", 0, 4000)
            if end <= 0:
                end = 4000
            pages.append(description[:end])
            description = description[end:].lstrip("\n")
        for index, page in enumerate(pages, 1):
            await send_admin_log(
                self.bot,
                channel_id=ROLE_LOG_CHANNEL_ID,
                title=title if len(pages) == 1 else f"{title} ({index}/{len(pages)})",
                description=page,
            )
