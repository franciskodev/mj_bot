import asyncio
import base64
from dataclasses import dataclass
from html import escape
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import asyncpg
import disnake

from config import TRANSCRIPT_INLINE_FILE_MAX_BYTES, TRANSCRIPT_INLINE_TOTAL_MAX_BYTES


ARCHIVE_ROOT = Path(__file__).resolve().parent / "data" / "applications"


@dataclass(frozen=True)
class SavedTranscript:
    html_path: Path
    json_path: Path
    message_count: int
    attachment_count: int
    linked_attachment_count: int


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


async def write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    await asyncio.to_thread(write_text_atomic, path, text)


def safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"http", "https"} and parsed.netloc and not any(ord(c) < 32 for c in value):
            return value
    except ValueError:
        pass
    return None


def link(url: Any, label: Any) -> str:
    safe = safe_url(url)
    text = escape(str(label))
    return f'<a href="{escape(safe, quote=True)}" target="_blank" rel="noopener noreferrer">{text}</a>' if safe else text


def inline_data(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(
        r"data:(?:image/(?:png|jpeg|gif|webp)|application/octet-stream);base64,[A-Za-z0-9+/=]+", value
    ):
        return value
    return None


def timestamp(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")
    except (ValueError, TypeError):
        return str(value or "—")


def format_text(value: str) -> str:
    """Render a small, escaped subset of Discord formatting without accepting HTML."""
    pattern = r"`[^`\n]+`|\*\*[^*]+\*\*|\[[^\]\n]+\]\(https?://[^\s)]+\)|https?://[^\s<>]+"
    parts = []
    offset = 0
    for match in re.finditer(pattern, value):
        parts.append(escape(value[offset:match.start()]))
        token = match.group()
        if token.startswith("`"):
            parts.append(f"<code>{escape(token[1:-1])}</code>")
        elif token.startswith("**"):
            parts.append(f"<strong>{escape(token[2:-2])}</strong>")
        elif token.startswith("["):
            label, url = token[1:-1].split("](", 1)
            parts.append(link(url, label))
        else:
            parts.append(link(token, token))
        offset = match.end()
    parts.append(escape(value[offset:]))
    return "".join(parts)


def render_embed(embed: dict[str, Any]) -> str:
    parts = ['<aside class="embed">']
    if embed.get("author", {}).get("name"):
        parts.append(f'<small>{escape(embed["author"]["name"])}</small>')
    if embed.get("title"):
        parts.append(f'<h3>{link(embed.get("url"), embed["title"])}</h3>')
    if embed.get("description"):
        parts.append(f'<div class="text">{format_text(embed["description"])}</div>')
    for field in embed.get("fields", []):
        parts.append(f'<div class="embed-field"><strong>{escape(str(field.get("name", "")))}</strong>'
                     f'<div class="text">{format_text(str(field.get("value", "")))}</div></div>')
    for key in ("image", "thumbnail", "video"):
        if embed.get(key, {}).get("url"):
            parts.append(f'<p>{link(embed[key]["url"], "Открыть медиа из карточки")}</p>')
    if embed.get("footer", {}).get("text"):
        parts.append(f'<small>{escape(embed["footer"]["text"])}</small>')
    return "".join(parts) + "</aside>"


def render_transcript(payload: dict[str, Any]) -> str:
    application = payload["application"]
    messages = payload["messages"]
    by_id = {str(message["id"]): message for message in messages}
    authors = {str(message["author"]["id"]): message["author"] for message in messages}
    applicant_id = str(application["discord_id"])
    reviewer_id = str(application.get("reviewer_id") or "")
    applicant = authors.get(applicant_id, {}).get("display_name") or application.get("username") or applicant_id
    reviewer = authors.get(reviewer_id, {}).get("display_name") or payload.get("reviewer_name") or reviewer_id or "Не указан"
    status = {"accepted": "Принят", "declined": "Отклонён", "review": "На рассмотрении",
              "interview": "Вызван на обзвон", "pending": "Ожидает решения"}.get(application["status"], application["status"])
    title = f'Заявка #{application["id"]} · INTERPOLOV'
    metadata = [
        ("Заявитель", f"{applicant} · {applicant_id}"),
        ("Игровой сервер", application.get("game_server") or "Не указан"),
        ("Результат", status), ("Рассмотрел", f"{reviewer} · {reviewer_id}" if reviewer_id else reviewer),
        ("Сообщений", str(len(messages))), ("Сохранено", timestamp(payload["saved_at"])),
    ]
    cards = "".join(f'<div><span>{escape(label)}</span><strong>{escape(value)}</strong></div>' for label, value in metadata)
    form_fields = [("Игровой ник", "game_nickname"), ("Статик", "static_id"),
                   ("Возраст OOC", "ooc_age"), ("Цель вступления", "goal")]
    form = "".join(f'<p><strong>{label}:</strong> {escape(str(application[key]))}</p>'
                   for label, key in form_fields if application.get(key) is not None)
    if application.get("screenshot_url"):
        form += f'<p>{link(application["screenshot_url"], "Фото персонажей из анкеты")}</p>'
    rendered = []
    for message in messages:
        author = message["author"]
        name = str(author.get("display_name") or author.get("username") or author["id"])
        avatar = inline_data(author.get("avatar_data"))
        avatar_html = (f'<img class="avatar" src="{avatar}" alt="" loading="lazy">' if avatar
                       else f'<div class="avatar placeholder" aria-hidden="true">{escape(name[:1].upper())}</div>')
        mid = escape(str(message["id"]), quote=True)
        badges = '<span class="badge">БОТ</span>' if author.get("bot") else ""
        if str(author["id"]) == applicant_id:
            badges += '<span class="badge applicant">ЗАЯВИТЕЛЬ</span>'
        if str(author["id"]) == reviewer_id:
            badges += '<span class="badge reviewer">РАССМОТРЕЛ</span>'
        parts = [f'<article class="message" id="message-{mid}">{avatar_html}<div class="body">',
                 f'<header><strong title="Discord ID: {escape(str(author["id"]), quote=True)}">{escape(name)}</strong>{badges}'
                 f'<a class="time" href="#message-{mid}">{escape(timestamp(message.get("created_at")))}</a></header>']
        reference_id = str(message.get("reference_message_id") or "")
        if reference_id:
            reference = by_id.get(reference_id)
            if reference:
                ref_name = reference["author"].get("display_name") or reference["author"].get("username", "")
                preview = (reference.get("display_content") or reference.get("content") or "Вложение / карточка")[:160]
                parts.append(f'<a class="reply" href="#message-{escape(reference_id, quote=True)}">'
                             f'↪ {escape(str(ref_name))}: {escape(preview)}</a>')
            else:
                parts.append(f'<div class="reply">↪ Ответ на сообщение {escape(reference_id)} (не вошло в архив)</div>')
        content = message.get("display_content", message.get("content", ""))
        if content:
            parts.append(f'<div class="text">{format_text(content)}</div>')
        for embed in message.get("embeds", []):
            parts.append(render_embed(embed))
        for attachment in message.get("attachments", []):
            filename = str(attachment.get("filename", "Вложение"))
            data = inline_data(attachment.get("data_uri"))
            if data:
                download = f'<a href="{data}" download="{escape(filename, quote=True)}">Скачать {escape(filename)}</a>'
                preview = f'<img class="attachment-image" src="{data}" alt="{escape(filename, quote=True)}" loading="lazy">' if data.startswith("data:image/") else ""
                note = "Встроено в HTML"
            else:
                download = link(attachment.get("url"), filename)
                preview = ""
                note = "Ссылка Discord; после её истечения файл может быть недоступен"
            parts.append(f'<div class="attachment">{preview}{download}<small>{escape(note)} · '
                         f'{int(attachment.get("size", 0)):,} байт</small></div>')
        for sticker in message.get("stickers", []):
            parts.append(f'<p>Стикер: {link(sticker.get("url"), sticker.get("name", "Стикер"))}</p>')
        if message.get("reactions"):
            parts.append('<div class="reactions">' + " ".join(
                f'<span>{escape(str(item["emoji"]))} {int(item["count"])}</span>' for item in message["reactions"]
            ) + '</div>')
        if message.get("edited_at"):
            parts.append(f'<small>Изменено: {escape(timestamp(message["edited_at"]))}</small>')
        if message.get("pinned"):
            parts.append('<small>📌 Закреплено</small>')
        parts.append('</div></article>')
        rendered.append("".join(parts))
    conversation = "".join(rendered) or '<p class="empty">В тикете нет сообщений.</p>'
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer"><title>{escape(title)}</title>
<style>
:root{{color-scheme:dark;--bg:#15171c;--panel:#20232b;--text:#e7e9ef;--muted:#a2abbd;--line:#343a48;--accent:#8fa8ff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1050px;margin:40px auto;padding:0 24px}}h1{{font-size:30px;margin:8px 0}}h2{{font-size:19px;margin:30px 0 16px}}h3{{font-size:16px;margin:4px 0 10px}}
.eyebrow{{font-size:12px;letter-spacing:.14em;color:var(--accent)}}.intro,small,.time{{color:var(--muted)}}small{{display:block;font-size:12px}}
a{{color:var(--accent);overflow-wrap:anywhere}}.summary{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:24px 0}}
.summary>div{{padding:16px;background:var(--panel);min-width:0}}.summary span{{display:block;color:var(--muted);font-size:12px}}.summary strong{{overflow-wrap:anywhere}}
details{{background:var(--panel);border:1px solid var(--line);padding:16px 20px;border-radius:12px}}summary{{cursor:pointer;font-weight:600}}details p{{white-space:pre-wrap;overflow-wrap:anywhere}}
.conversation{{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}}.message{{display:flex;gap:14px;padding:18px 22px;border-bottom:1px solid var(--line);scroll-margin-top:16px}}.message:last-child{{border:0}}.message:target{{background:#2b3350}}
.avatar{{width:40px;height:40px;border-radius:50%;flex-shrink:0}}.placeholder{{background:#3d4770;display:grid;place-items:center;font-weight:bold}}.body{{min-width:0;flex:1}}header{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:5px}}
.time{{font-size:11px;text-decoration:none}}.badge{{font-size:9px;padding:1px 5px;border-radius:4px;background:#424b70;color:#e7ecff}}.applicant{{background:#23534e}}.reviewer{{background:#674777}}
.text{{white-space:pre-wrap;overflow-wrap:anywhere}}code{{background:#11141b;border-radius:4px;padding:2px 4px}}.reply{{display:block;border-left:3px solid #67789f;padding:4px 10px;margin:6px 0 10px;font-size:12px;color:var(--muted);text-decoration:none;overflow-wrap:anywhere}}
.embed{{border-left:4px solid #7d91d4;background:#191c23;padding:14px 18px;margin:12px 0;border-radius:5px}}.embed-field{{margin:10px 0}}.embed-field strong{{font-size:13px}}.attachment{{border:1px solid var(--line);border-radius:8px;padding:12px;margin-top:10px}}.attachment-image{{display:block;max-width:100%;max-height:480px;border-radius:6px;margin-bottom:10px}}
.reactions{{margin:10px 0}}.reactions span{{display:inline-block;border:1px solid var(--line);border-radius:6px;padding:0 7px;font-size:12px}}.empty{{padding:20px}}footer{{font-size:12px;color:var(--muted);padding:22px 0}}
@media(max-width:650px){{main{{margin:20px auto;padding:0 12px}}.summary{{grid-template-columns:1fr 1fr}}.message{{padding:14px 12px;gap:10px}}h1{{font-size:24px}}}}
@media print{{:root{{color-scheme:light;--bg:#fff;--panel:#fff;--text:#111;--muted:#555;--line:#ddd;--accent:#24499c}}.embed{{background:#f6f6f6}}.message{{break-inside:avoid}}}}
</style></head><body><main><div class="eyebrow">INTERPOLOV / АРХИВ ЗАЯВОК</div><h1>{escape(title)}</h1>
<p class="intro">#{escape(str(payload["channel"]["name"]))} · Переписка в хронологическом порядке · Поиск: Ctrl+F</p>
<section class="summary">{cards}</section><details><summary>Анкета участника</summary>{form}</details>
<h2>Переписка · {len(messages)} сообщений</h2><section class="conversation">{conversation}</section>
<footer>Локальная копия переписки на момент сохранения. Время указано в UTC. Ответы ведут к сообщениям внутри файла.
Встроенные вложения доступны без Discord; внешние ссылки могут истечь. Удалённые до сохранения сообщения не восстанавливаются.</footer>
</main></body></html>'''


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
) -> SavedTranscript:
    html_path = ARCHIVE_ROOT / f"application-{application['id']}-transcript.html"
    json_path = html_path.with_suffix(".json")
    messages: list[dict[str, Any]] = []
    upload_limit = channel.guild.filesize_limit
    budget = min(TRANSCRIPT_INLINE_TOTAL_MAX_BYTES, max(0, (upload_limit - 512_000) * 3 // 8))
    avatars: dict[int, str | None] = {}

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
        for attachment, stored in zip(message.attachments, attachments):
            if attachment.size > min(TRANSCRIPT_INLINE_FILE_MAX_BYTES, budget):
                continue
            try:
                data = await asyncio.wait_for(attachment.read(), timeout=15)
            except Exception:
                logging.warning("Could not embed attachment %s in application %s", attachment.id, application["id"])
                continue
            if len(data) > min(TRANSCRIPT_INLINE_FILE_MAX_BYTES, budget):
                continue
            mime = (attachment.content_type or "").split(";", 1)[0].lower()
            if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                mime = "application/octet-stream"
            stored["data_uri"] = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
            budget -= len(data)

        author_id = message.author.id
        if author_id not in avatars:
            avatars[author_id] = None
            if budget > 0:
                try:
                    avatar = message.author.display_avatar.with_size(64).with_format("png")
                    data = await asyncio.wait_for(avatar.read(), timeout=10)
                    if len(data) <= min(budget, 128_000):
                        avatars[author_id] = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
                        budget -= len(data)
                except Exception:
                    logging.warning("Could not embed avatar for author %s", author_id)

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
                    "avatar_data": avatars[author_id],
                },
                "content": message.content,
                "display_content": message.clean_content or message.system_content,
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

    reviewer = channel.guild.get_member(application["reviewer_id"]) if application.get("reviewer_id") else None
    payload = {
        "format_version": 2,
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
        "reviewer_name": reviewer.display_name if reviewer is not None else None,
    }
    html = await asyncio.to_thread(render_transcript, payload)
    if len(html.encode("utf-8")) > upload_limit:
        # Keep the conversation even when media would exceed Discord's upload limit.
        for message in messages:
            message["author"]["avatar_data"] = None
            for attachment in message["attachments"]:
                attachment.pop("data_uri", None)
        html = await asyncio.to_thread(render_transcript, payload)
    await write_json(json_path, payload)
    await asyncio.to_thread(write_text_atomic, html_path, html)
    attachments = [attachment for message in messages for attachment in message["attachments"]]
    return SavedTranscript(
        html_path=html_path, json_path=json_path, message_count=len(messages),
        attachment_count=len(attachments),
        linked_attachment_count=sum(not attachment.get("data_uri") for attachment in attachments),
    )
