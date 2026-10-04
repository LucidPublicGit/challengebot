"""Security boundary regression checks; no live Discord or production database."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from fastapi.security import HTTPBasicCredentials

from app.achievement_commands import achievement_admin, add, edit, remove
from app.config import Settings
from app.services import achievements as ach
from app.web.app import _auth, create_app
from tests.helpers import cleanup_db, setup_tmp_db


def test_admin_http_security():
    with patch("app.web.app.get_settings", return_value=Settings()):
        client = TestClient(create_app())
        assert client.get("/").status_code == 503
        assert client.get("/api/achievements").status_code == 503
        assert client.get("/health").json() == {"ok": True}
        assert client.get("/openapi.json").status_code == 404
    with patch("app.web.app.get_settings", return_value=Settings(ui_password="sëcret")):
        _auth(HTTPBasicCredentials(username="admin", password="sëcret"))
        client = TestClient(create_app())
        client.auth = ("admin", "sëcret")
        # Same-origin protection happens before any mutation or database access.
        for headers in ({}, {"Origin": "https://evil.example"},
                        {"Origin": "null"}, {"Origin": "http://["},
                        {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"}):
            assert client.post("/backup/run", headers=headers).status_code == 403
        assert client.get("/health").headers["x-frame-options"] == "DENY"
        assert client.get("/health").headers["cache-control"] == "no-store"


def test_achievement_slash_security_and_crud():
    path = setup_tmp_db()
    try:
        async def check():
            interaction = SimpleNamespace(
                guild=object(), response=SimpleNamespace(
                    send_message=AsyncMock(), defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
            )
            with patch("app.bot.is_bot_admin", return_value=False):
                assert not await achievement_admin.interaction_check(interaction)
                interaction.response.send_message.assert_awaited_once()
                assert len(ach.list_definitions()) == 18
            with patch("app.bot.is_bot_admin", return_value=True):
                assert await achievement_admin.interaction_check(interaction)
                interaction.guild = None
                assert not await achievement_admin.interaction_check(interaction)
                interaction.guild = object()
            assert achievement_admin.guild_only
            assert {c.name for c in achievement_admin.commands} == {"add", "edit", "remove"}
            await add.callback(interaction, "New", "One clear.", '{"kind":"clears","count":1}',
                               "achievement-001.png")
            item = next(d for d in ach.list_definitions() if d["title"] == "New")
            aid = item["id"]
            await edit.callback(interaction, aid, title="Renamed")
            saved = next(d for d in ach.list_definitions() if d["id"] == aid)
            assert saved["title"] == "Renamed" and saved["rule_json"] == item["rule_json"]
            await edit.callback(interaction, aid, image_file="../private.png")
            assert next(d for d in ach.list_definitions() if d["id"] == aid)["image_file"] == item["image_file"]
            await edit.callback(interaction, "missing", title="Not created")
            assert len(ach.list_definitions()) == 19
            await remove.callback(interaction, aid, False)
            assert len(ach.list_definitions()) == 19
            await remove.callback(interaction, aid, True)
            assert len(ach.list_definitions()) == 18
            await edit.callback(interaction, aid, published=True)
            assert len(ach.list_definitions()) == 19
        asyncio.run(check())
    finally:
        cleanup_db(path)
