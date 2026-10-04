"""Announcement composer checks without a live Discord connection."""

import re
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.catalog_changes import list_changes
from app.web.app import create_app
from tests.helpers import cleanup_db, setup_tmp_db


def _token(response):
    return re.search(r'name="token" value="([^"]+)"', response.text).group(1)


def _target():
    return SimpleNamespace(
        guild=SimpleNamespace(filesize_limit=10 * 1024 * 1024),
        name="news", send=AsyncMock(),
    )


def _bot():
    return SimpleNamespace(guilds=[], is_ready=lambda: True)


def test_web_announcement_draft_and_access():
    path = setup_tmp_db()
    try:
        with patch("app.web.app.get_settings", return_value=Settings(ui_password="secret")):
            client = TestClient(create_app())
            assert client.get("/announcements").status_code == 401
            assert client.get("/announcements/physical-image").status_code == 401
            page = client.get("/announcements?preset=physical", auth=("admin", "secret"))
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-store"
            assert "Walking Immersion Easy" in page.text
            assert "Manga 3" not in page.text
            assert 'value="physical" selected' in page.text
            assert "Publish announcement" in page.text and _token(page)
            asset = client.get("/announcements/physical-image", auth=("admin", "secret"))
            assert asset.status_code == 200 and asset.headers["content-type"] == "image/png"
    finally:
        cleanup_db(path)


def test_web_announcement_publish_and_one_use():
    path = setup_tmp_db()
    try:
        target = _target()
        resolver = AsyncMock(return_value=target)
        with patch("app.web.app.get_settings", return_value=Settings(ui_password="secret")), \
             patch("app.web.announcements.resolve_announce_channel", resolver):
            client = TestClient(create_app(_bot()), headers={"Origin": "http://testserver"})
            client.auth = ("admin", "secret")
            data = {
                "title": "Community news", "message": "@everyone\n" + "a" * 4100,
                "image": "physical", "confirmed": "yes",
                "token": _token(client.get("/announcements")),
            }
            response = client.post("/announcements", data=data)
            assert response.status_code == 200 and "Published to #news." in response.text
            calls = target.send.call_args_list
            assert calls[0].kwargs["file"].filename == "physical-option-6.png"
            assert calls[0].kwargs["embed"].title == "Community news"
            assert "".join(c.args[0] for c in calls[1:]) == data["message"]
            assert all(c.kwargs["allowed_mentions"].everyone is False for c in calls)
            count = target.send.await_count
            assert client.post("/announcements", data=data).status_code == 403
            assert client.post("/announcements", data={**data, "token": ""}).status_code == 403
            assert target.send.await_count == count
            resolver.assert_awaited_once()
    finally:
        cleanup_db(path)


def test_web_announcement_upload_and_errors():
    path = setup_tmp_db()
    try:
        target = _target()
        with patch("app.web.app.get_settings", return_value=Settings(ui_password="secret")), \
             patch("app.web.announcements.resolve_announce_channel", AsyncMock(return_value=target)):
            client = TestClient(create_app(_bot()), headers={"Origin": "http://testserver"})
            client.auth = ("admin", "secret")

            def post(**overrides):
                data = {
                    "token": _token(client.get("/announcements")), "title": "<script>news</script>",
                    "message": "Updated or retired challenges", "confirmed": "yes", "image": "none",
                }
                files = overrides.pop("files", None)
                return client.post("/announcements", data={**data, **overrides}, files=files)

            assert post(confirmed="").status_code == 400
            assert post(image="../private").status_code == 400
            assert post(channel_id="not-a-channel").status_code == 400
            response = post(message="")
            assert response.status_code == 400 and "&lt;script&gt;news&lt;/script&gt;" in response.text
            assert "<script>news</script>" not in response.text
            assert post(image="upload", files={"upload": ("x.png", b"bad image", "image/png")}).status_code == 400
            target.send.assert_not_awaited()
            png = BytesIO()
            Image.new("RGB", (2, 2), "red").save(png, "PNG")
            result = post(image="upload", channel_id="123", files={
                "upload": ("../../unsafe-name.svg", png.getvalue(), "application/octet-stream")
            })
            assert result.status_code == 200
            assert target.send.call_args_list[0].kwargs["file"].filename == "announcement.png"
            target.send.reset_mock()
            target.send.side_effect = discord.HTTPException(
                SimpleNamespace(status=403, reason="Forbidden"), "No permissions"
            )
            failed = post()
            assert failed.status_code == 502 and "after 0 message(s)" in failed.text
            target.send.side_effect = [
                SimpleNamespace(id=1),
                discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "No permissions"),
            ]
            failed = post()
            assert failed.status_code == 502 and "after 1 message(s)" in failed.text
            disconnected = TestClient(create_app(), headers={"Origin": "http://testserver"})
            disconnected.auth = ("admin", "secret")
            result = disconnected.post("/announcements", data={
                "token": _token(disconnected.get("/announcements")),
                "title": "News", "message": "News", "confirmed": "yes",
            })
            assert result.status_code == 400 and "not ready" in result.text
    finally:
        cleanup_db(path)


def test_change_draft_marks_only_successful_publications():
    path = setup_tmp_db()
    try:
        target = _target()
        with patch("app.web.app.get_settings", return_value=Settings(ui_password="secret")), \
             patch("app.web.announcements.resolve_announce_channel", AsyncMock(return_value=target)):
            client = TestClient(create_app(_bot()), headers={"Origin": "http://testserver"})
            client.auth = ("admin", "secret")
            page = client.get("/announcements?preset=changes")
            assert "Manga 3" in page.text and "Walking Immersion Easy" in page.text
            assert "12 tracked changes" in page.text
            assert 'value="physical" selected' in page.text
            assert len(list_changes(pending_only=True)) == 12
            target.send.side_effect = discord.HTTPException(
                SimpleNamespace(status=403, reason="Forbidden"), "No permissions"
            )
            data = {
                "token": _token(page), "title": "Catalog updates", "message": "Reviewed updates",
                "confirmed": "yes", "image": "none",
            }
            response = client.post("/announcements", data=data)
            assert response.status_code == 502
            assert len(list_changes(pending_only=True)) == 12
            target.send.side_effect = None
            data["token"] = _token(response)
            # Changes created after the preview must remain pending.
            from app.catalog import get_challenge
            from app.services.attempts import admin_upsert_challenge
            admin_upsert_challenge({**get_challenge("easy-2"), "title": "Later edit"})
            response = client.post("/announcements", data=data)
            assert response.status_code == 200
            pending = list_changes(pending_only=True)
            assert len(pending) == 1 and pending[0]["after"]["title"] == "Later edit"
            history = client.get("/announcements/history")
            assert history.status_code == 200 and "Definition snapshots" in history.text
    finally:
        cleanup_db(path)
