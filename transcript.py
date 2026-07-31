import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import asyncpg
import disnake


ARCHIVE_ROOT = Path(__file__).resolve().parent / "data" / "applications"


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


async def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    await asyncio.to_thread(path.write_text, text, encoding="utf-8")


async def save_application_json(
    application: asyncpg.Record,
    previous: list[asyncpg.Record],
) -> Path:
    path = ARCHIVE_ROOT / f"application-{application['id']}.json"
    payload = {
        "format_version": 1,
        "type": "family_application",
        "saved_at": disnake.utils.utcnow().isoformat(),
        "application": json_value(dict(application)),
        "previous_applications": [json_value(dict(item)) for item in previous],
    }
    await write_json(path, payload)
    return path


async def save_ticket_transcript(
    channel: disnake.TextChannel,
    application: asyncpg.Record,
) -> Path:
    application_path = ARCHIVE_ROOT / f"application-{application['id']}.json"
    messages: list[dict[str, Any]] = []

    async for message in channel.history(limit=None, oldest_first=True):
        attachments = [
            {
                "id": attachment.id,
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "size": attachment.size,
                "url": attachment.url,
                "proxy_url": attachment.proxy_url,
            }
            for attachment in message.attachments
        ]

        components = []
        for component in message.components:
            try:
                components.append(component.to_dict())
            except (AttributeError, TypeError):
                components.append({"text": str(component)})

        messages.append(
            {
                "id": message.id,
                "author": {
                    "id": message.author.id,
                    "username": str(message.author),
                    "display_name": getattr(message.author, "display_name", str(message.author)),
                    "bot": message.author.bot,
                },
                "content": message.content,
                "created_at": message.created_at.isoformat(),
                "edited_at": message.edited_at.isoformat() if message.edited_at else None,
                "pinned": message.pinned,
                "tts": message.tts,
                "jump_url": message.jump_url,
                "attachments": attachments,
                "embeds": [embed.to_dict() for embed in message.embeds],
                "components": components,
                "stickers": [
                    {
                        "id": sticker.id,
                        "name": sticker.name,
                        "url": str(sticker.url),
                    }
                    for sticker in message.stickers
                ],
                "reactions": [
                    {
                        "emoji": str(reaction.emoji),
                        "count": reaction.count,
                        "me": reaction.me,
                    }
                    for reaction in message.reactions
                ],
                "reference_message_id": (
                    message.reference.message_id if message.reference else None
                ),
            }
        )

    transcript_path = application_path
    payload = {
        "format_version": 1,
        "type": "discord_ticket_transcript",
        "saved_at": disnake.utils.utcnow().isoformat(),
        "application": json_value(dict(application)),
        "channel": {
            "id": channel.id,
            "name": channel.name,
            "guild_id": channel.guild.id,
        },
        "message_count": len(messages),
        "messages": messages,
    }
    await write_json(transcript_path, payload)
    return transcript_path
