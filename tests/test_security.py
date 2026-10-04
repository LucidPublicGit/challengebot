"""Security boundary regression checks; no live Discord or production database."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx

from fastapi.testclient import TestClient
from fastapi.security import HTTPBasicCredentials

from app.achievement_commands import achievement_admin, add, edit, remove
from app.config import Settings
from app import congrats, poster
from app.bot import is_bot_admin
from app.bot_commands import AdminPanelView, challenge_announce
from app.services import achievements as ach
from app.web.app import _auth, create_app
from tests.helpers import cleanup_db, setup_tmp_db


def test_admin_is_scoped_to_configured_guild():
    member = MagicMock(spec=discord.Member)
    member.id = 123
    member.guild_permissions.administrator = True
    member.roles = []
    interaction = SimpleNamespace(user=member, guild=SimpleNamespace(id=456, owner_id=123))
    with patch("app.bot._runtime", return_value={}), patch(
        "app.bot.get_settings", return_value=Settings(discord_guild_id="456")
    ):
        assert is_bot_admin(interaction)
        interaction.guild.id = 789
        assert not is_bot_admin(interaction)  # Even the other server's owner.
        interaction.guild = None
        assert not is_bot_admin(interaction)
    interaction.guild = SimpleNamespace(id=456, owner_id=999)
    with patch("app.bot.get_settings", return_value=Settings()), patch(
        "app.bot._runtime", return_value={}
    ):
        assert not is_bot_admin(interaction)
    with patch("app.bot.get_settings", return_value=Settings()), patch(
        "app.bot._runtime", return_value={"guild_id": "456", "admin_role_ids": ["42"]}
    ):
        assert is_bot_admin(interaction)
        member.guild_permissions.administrator = False
        member.roles = [SimpleNamespace(id=42)]
        assert is_bot_admin(interaction)
        member.roles = []
        assert not is_bot_admin(interaction)


def test_admin_panel_rechecks_revoked_permissions():
    async def check():
        interaction = SimpleNamespace(user=SimpleNamespace(id=123),
                                      response=SimpleNamespace(send_message=AsyncMock()))
        view = AdminPanelView(123, interaction.user, None)
        with patch("app.bot.is_bot_admin", return_value=False):
            assert not await view.interaction_check(interaction)
        with patch("app.bot.is_bot_admin", return_value=True):
            assert await view.interaction_check(interaction)
            interaction.user.id = 999
            assert not await view.interaction_check(interaction)
    asyncio.run(check())


def test_announcement_rejects_forged_choices():
    async def check():
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("app.bot.is_bot_admin", return_value=True), patch(
            "app.bot_commands.discord.File"
        ) as file:
            for values in ({"image": -1}, {"image": "../../private"},
                           {"change": "invalid"}, {"category": "invalid"}):
                await challenge_announce.callback(interaction, message="Test", **values)
            file.assert_not_called()
            assert interaction.followup.send.await_count == 4
    asyncio.run(check())


def test_congrats_cannot_read_outside_collection():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "pics"
        root.mkdir()
        outside = Path(td) / "private.gif"
        outside.write_bytes(b"secret" * 100)
        with patch("app.congrats.congrats_root", return_value=root):
            for tier in ("..", "../", str(Path(td)), "..\\"):
                assert congrats.pick_congrats(tier=tier, mark=False) is None
            assert congrats._iter_dir_images(Path(td)) == []
            assert congrats.frame_congrats(outside) is None
            try:
                congrats.as_discord_congrats(outside)
            except ValueError:
                pass
            else:
                raise AssertionError("Outside file was attached")
            (root / "easy").mkdir()
            (root / "easy" / "ok.gif").write_bytes(b"valid" * 100)
            with patch("app.congrats._usage_map", return_value={}):
                assert congrats.pick_congrats(tier="easy", mark=False).name == "ok.gif"


def test_congrats_rejects_external_symlinks(tmp_path):
    root = tmp_path / "pics"
    root.mkdir()
    outside = tmp_path / "private.gif"
    outside.write_bytes(b"secret")
    try:
        (root / "linked.gif").symlink_to(outside)
    except OSError:
        # Windows may require elevation for symlinks; check the resolved target
        # boundary without needing that privilege.
        link = MagicMock(spec=Path)
        link.resolve.return_value = outside
        with patch("app.congrats.congrats_root", return_value=root):
            assert not congrats._is_media(link)
            link.is_file.assert_not_called()
        return
    with patch("app.congrats.congrats_root", return_value=root):
        assert congrats._general_pool(root) == []
        assert congrats._walk_media(root) == []


def test_poster_download_boundary():
    async def check():
        requests = []

        def handle(request):
            requests.append(str(request.url))
            return httpx.Response(200, content=b"x" * 200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                    follow_redirects=True) as client:
            for url in ("http://s4.anilist.co/a", "https://127.0.0.1/a",
                        "https://[::1]/a", "file:///etc/passwd",
                        "https://s4.anilist.co.evil.example/a",
                        "https://user:pass@s4.anilist.co/a",
                        "https://s4.anilist.co:8090/a"):
                assert await poster._download(client, url) is None
            assert not requests
            assert await poster._download(client, "https://s4.anilist.co/a") == b"x" * 200
        requests.clear()

        def redirect(request):
            requests.append(str(request.url))
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect),
                                    follow_redirects=True) as client:
            assert await poster._download(client, "https://s4.anilist.co/a") is None
            assert requests == ["https://s4.anilist.co/a"]

        class OversizedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * (64 * 1024)
                yield b"x" * (64 * 1024)
                raise AssertionError("Download kept reading after its limit")

        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=OversizedStream())
        )) as client:
            with patch("app.poster._MAX_IMAGE_BYTES", 64 * 1024):
                assert await poster._download(client, "https://s4.anilist.co/a") is None
    asyncio.run(check())


def test_admin_http_security():
    with patch("app.web.app.get_settings", return_value=Settings()):
        client = TestClient(create_app())
        assert client.get("/").status_code == 503
        assert client.get("/api/achievements").status_code == 503
        h = client.get("/health")
        assert h.status_code == 503
        assert h.json() == {"ok": False, "discord": False}
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


def test_health_ok_when_discord_ready():
    bot = SimpleNamespace(is_ready=lambda: True)
    with patch("app.web.app.get_settings", return_value=Settings()):
        client = TestClient(create_app(bot))
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "discord": True}


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
