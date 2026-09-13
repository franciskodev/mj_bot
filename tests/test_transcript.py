import base64
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import disnake

from cogs.applications import ApplicationsCog
from config import GUILD_ID
from transcript import SavedTranscript, render_transcript, save_ticket_transcript


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def application():
    return {"id": 42, "discord_id": 10, "username": "Applicant", "game_server": "Seattle",
            "status": "accepted", "reviewer_id": 20, "created_at": NOW,
            "guild_id": GUILD_ID, "ticket_channel_id": 100, "ticket_message_id": 200,
            "transcript_log_message_id": None}


def message(mid, content, *, reference=None, attachments=()):
    avatar = SimpleNamespace(read=AsyncMock(return_value=PNG))
    avatar.with_size = lambda size: avatar
    avatar.with_format = lambda format: avatar
    return SimpleNamespace(
        id=mid, author=SimpleNamespace(id=10, display_name="Applicant", bot=False, display_avatar=avatar),
        content=content, clean_content=content, system_content=content, created_at=NOW, edited_at=None,
        pinned=False, tts=False, jump_url=f"https://discord.com/channels/{GUILD_ID}/100/{mid}",
        attachments=list(attachments), embeds=[], components=[], stickers=[], reactions=[],
        reference=SimpleNamespace(message_id=reference) if reference else None,
    )


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class TranscriptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        patcher = patch("transcript.ARCHIVE_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.channel = SimpleNamespace(
            id=100, name="ticket-42", guild=SimpleNamespace(id=GUILD_ID, filesize_limit=8 * 1024 * 1024,
                                                          get_member=lambda uid: None),
        )

    def history(self, messages):
        async def iterate(**kwargs):
            self.assertEqual(kwargs, {"limit": None, "oldest_first": True})
            for item in messages:
                yield item
        self.channel.history = iterate

    def attachment(self, size=None):
        return SimpleNamespace(id=50, filename='photo.png', content_type='image/png',
                               size=len(PNG) if size is None else size,
                               url='https://cdn.discordapp.com/photo.png', proxy_url='https://cdn.discordapp.com/photo.png',
                               read=AsyncMock(return_value=PNG))

    async def test_saves_html_json_replies_and_embedded_files_without_overwriting_application(self):
        original = self.root / "application-42.json"
        original.write_text('{"original": true}', encoding="utf-8")
        first = message(1, "Первое сообщение", attachments=[self.attachment()])
        second = message(2, "Ответ на вопрос", reference=1)
        second.author.id, second.author.display_name = 20, "Recruiter"
        second.edited_at = NOW
        self.history([first, second])
        saved = await save_ticket_transcript(self.channel, application())
        self.assertEqual(saved.message_count, 2)
        self.assertEqual(saved.attachment_count, 1)
        self.assertEqual(saved.linked_attachment_count, 0)
        self.assertEqual(saved.html_path.suffix, ".html")
        self.assertEqual(json.loads(original.read_text()), {"original": True})
        payload = json.loads(saved.json_path.read_text())
        self.assertEqual(payload["message_count"], 2)
        self.assertEqual(payload["messages"][1]["reference_message_id"], 1)
        html = saved.html_path.read_text()
        self.assertIn('href="#message-1"', html)
        self.assertIn("data:image/png;base64,", html)
        self.assertIn('download="photo.png"', html)
        self.assertIn("Recruiter", html)
        self.assertIn("Изменено:", html)

    async def test_unavailable_and_large_files_keep_links(self):
        large = self.attachment(size=10 * 1024 * 1024)
        failed = self.attachment()
        failed.read.side_effect = disnake.DiscordException("expired")
        self.history([message(1, "Files", attachments=[large, failed])])
        with self.assertLogs(level="WARNING"):
            saved = await save_ticket_transcript(self.channel, application())
        large.read.assert_not_awaited()
        self.assertEqual(saved.linked_attachment_count, 2)
        self.assertIn("Ссылка Discord", saved.html_path.read_text())

    async def test_no_messages_still_produces_summary(self):
        self.history([])
        saved = await save_ticket_transcript(self.channel, application())
        self.assertEqual(saved.message_count, 0)
        self.assertIn("В тикете нет сообщений", saved.html_path.read_text())

    async def test_html_and_script_urls_in_user_content_cannot_execute(self):
        attack = '<script>alert(1)</script><img src=x onerror="alert(2)">'
        item = message(1, attack)
        item.author.display_name = attack
        item.embeds = [disnake.Embed(title=attack, description=attack, url="javascript:alert(1)")]
        file = self.attachment(size=100 * 1024 * 1024)
        file.url, file.filename = "javascript:alert(3)", 'x" onclick="alert(4)'
        item.attachments = [file]
        self.history([item])
        saved = await save_ticket_transcript(self.channel, application())
        parser = Tags()
        html = saved.html_path.read_text()
        parser.feed(html)
        self.assertIn("&lt;script&gt;", html)
        for tag, attributes in parser.tags:
            self.assertNotIn(tag, {"script", "iframe", "object"})
            self.assertFalse(any(key.startswith("on") for key in attributes))
            self.assertFalse(attributes.get("href", "").startswith("javascript:"))
            if tag == "img":
                self.assertTrue(attributes["src"].startswith("data:image/"))

    async def test_backup_can_render_without_discord(self):
        self.history([message(1, "Offline")])
        saved = await save_ticket_transcript(self.channel, application())
        self.assertEqual(render_transcript(json.loads(saved.json_path.read_text())), saved.html_path.read_text())


class TranscriptDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        path = Path(self.temporary.name) / "transcript.html"
        path.write_text("<!doctype html><html lang=ru>Transcript</html>")
        self.saved = SavedTranscript(path, path.with_suffix(".json"), 5, 2, 1)
        self.db = SimpleNamespace(set_application_transcript_log_message=AsyncMock(),
                                  get_application_config=AsyncMock(return_value={}),
                                  get_application=AsyncMock(side_effect=lambda _: application()))
        self.cog = ApplicationsCog(SimpleNamespace(database=self.db))
        self.channel = Mock(spec=disnake.TextChannel)
        self.channel.send = AsyncMock()
        self.channel.delete = AsyncMock()
        self.archive = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=900)))
        patcher = patch("cogs.applications.application_log_channel", AsyncMock(return_value=self.archive))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_archive_receives_html_and_summary_with_no_pings(self):
        self.assertTrue(await self.cog._upload_ticket_transcript(self.channel, application(), "Принят", self.saved))
        sent = self.archive.send.call_args.kwargs
        self.assertEqual(sent["file"].filename, "application-42.html")
        fields = {field.name: field.value for field in sent["embed"].fields}
        self.assertEqual(fields["Сообщений"], "5")
        self.assertEqual(fields["Игровой сервер"], "Seattle")
        self.assertEqual(fields["Рассмотрел"], "<@20>")
        self.assertEqual(sent["allowed_mentions"].to_dict()["parse"], [])
        self.db.set_application_transcript_log_message.assert_awaited_once_with(42, 900)

    async def test_refresh_replaces_existing_archive_file(self):
        record = application() | {"transcript_log_message_id": 900}
        old = SimpleNamespace(id=900, edit=AsyncMock())
        self.archive.fetch_message = AsyncMock(return_value=old)
        self.assertTrue(await self.cog._upload_ticket_transcript(self.channel, record, "Принят", self.saved))
        self.archive.send.assert_not_awaited()
        self.assertEqual(old.edit.call_args.kwargs["attachments"], [])
        self.assertEqual(old.edit.call_args.kwargs["file"].filename, "application-42.html")

    async def test_failed_upload_does_not_enable_deletion(self):
        self.cog._save_ticket_transcript_with_retry = AsyncMock(return_value=self.saved)
        self.cog._ensure_ticket_delete_prompt = AsyncMock()
        self.archive.send.side_effect = disnake.DiscordException("denied")
        with self.assertLogs(level="ERROR"):
            self.assertFalse(await self.cog._finalize_ticket(self.channel, application(), "Принят", "Ready"))
        self.cog._ensure_ticket_delete_prompt.assert_not_awaited()
        self.db.set_application_transcript_log_message.assert_not_awaited()

    async def test_insecure_archive_does_not_receive_transcript(self):
        with patch("cogs.applications.application_log_channel", AsyncMock(return_value=None)):
            self.assertFalse(await self.cog._upload_ticket_transcript(self.channel, application(), "Принят", self.saved))
        self.archive.send.assert_not_awaited()

    async def test_delete_reexports_and_aborts_when_archiving_fails(self):
        inter = SimpleNamespace(
            component=SimpleNamespace(custom_id="application:delete:42"), author=Mock(spec=disnake.Member),
            guild_id=GUILD_ID, channel_id=100, channel=self.channel,
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
        )
        self.cog._finalize_ticket = AsyncMock(return_value=False)
        with patch("cogs.applications.can_review_application", AsyncMock(return_value=True)):
            await self.cog.application_button(inter)
        self.cog._finalize_ticket.assert_awaited_once()
        self.channel.delete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
