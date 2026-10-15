"""Audit regressions against real ASGI parsing and SQLite, with no network."""
import asyncio
import base64
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from app.bot import GuildCommandTree
from app.challenge_card import _poster_image
from app.config import Settings
from app.http_limits import bounded_request
from app.services import attempts as att, progress_status as ps
from app.web.app import create_app
from app.web.security import BODY_LIMITS, FORM_LIMIT
from tests.helpers import setup_tmp_db, cleanup_db


def test_authentication_and_total_body_limits_precede_form_parsing():
    async def check():
        for password, auth, expected in (("test", None, 401), ("test", "bad", 401),
                                         ("", None, 503)):
            with patch("app.web.app.get_settings", return_value=Settings(ui_password=password)):
                app = create_app()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://testserver") as client:
                    async def unread():
                        raise AssertionError("Unauthenticated body was consumed")
                        yield b""
                    headers = {"Origin": "http://testserver"}
                    if auth:
                        headers["Authorization"] = "Basic " + base64.b64encode(b"admin:bad").decode()
                    for path in ("/achievements", "/announcements", "/settings"):
                        response = await client.post(path, headers=headers, content=unread())
                        assert response.status_code == expected
        with patch("app.web.app.get_settings", return_value=Settings(ui_password="test")):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
                                         base_url="http://testserver", auth=("admin", "test")) as client:
                for path, limit in (("/settings", FORM_LIMIT), ("/achievements/fixture", BODY_LIMITS["/achievements"]), *BODY_LIMITS.items()):
                    consumed = 0
                    async def oversized():
                        nonlocal consumed
                        for _ in range(limit // 65536 + 2):
                            consumed += 65536
                            yield b"x" * 65536
                        raise AssertionError("Continued reading beyond the byte ceiling")
                    for declared in (False, True):
                        consumed = 0
                        headers = {"Origin": "http://testserver", "Content-Type":
                                   "multipart/form-data; boundary=test"}
                        if declared:
                            headers["Content-Length"] = str(limit + 1)
                        r = await client.post(path, headers=headers, content=oversized())
                        assert r.status_code == 413
                        assert consumed == (0 if declared else limit + 65536)
    asyncio.run(check())


def test_tadoku_link_works_immediately_and_relink_races_are_rejected():
    path = setup_tmp_db()
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    try:
        async def check():
            att.link_tadoku("audit", uid)
            assert att.start_attempt("audit", tier="easy", number=4, param="Game", guild_id="g")["ok"]
            logs = [{"id": "fixture", "amount": 40000, "unit": "characters",
                     "activity": "reading", "tags": ["vn"], "description": "Game",
                     "created_at": datetime.now(timezone.utc).isoformat()}]
            with patch("app.services.attempts.resolve_logs_for_user", new_callable=AsyncMock,
                       return_value=logs) as fetch, patch("app.tadoku.client.resolve_logs_for_user",
                                                        new_callable=AsyncMock, return_value=logs):
                assert (await att.confirm_attempt("audit", dry_run=True))["ok"]
                row = ps.list_active_attempts()[0]
                meta = {}
                await ps.check_goal_milestones(row, meta, force=True)
                assert meta["goal_ok"]

                async def relink_during_fetch(**kwargs):
                    att.link_tadoku("audit", "11111111-1111-1111-1111-111111111111")
                    return logs
                fetch.side_effect = relink_during_fetch
                assert not (await att.confirm_attempt("audit"))["ok"]
                assert att.active_attempt("audit")
                fetch.side_effect = None
                completed = await att.confirm_attempt("audit")
                assert completed["ok"] and completed["method"] == "tadoku_poll"
                assert not att.active_attempt("audit")
        asyncio.run(check())
    finally:
        cleanup_db(path)


def test_member_command_boundary_rejects_dms_and_foreign_guilds():
    async def check():
        interaction = SimpleNamespace(guild=None, response=SimpleNamespace(send_message=AsyncMock()))
        with patch("app.bot.get_settings", return_value=Settings(discord_guild_id="456")):
            for guild in (None, SimpleNamespace(id=789)):
                interaction.guild = guild
                assert not await GuildCommandTree.interaction_check(None, interaction)
            interaction.guild = SimpleNamespace(id=456)
            assert await GuildCommandTree.interaction_check(None, interaction)
        with patch("app.bot.get_settings", return_value=Settings()), patch("app.bot._runtime", return_value={}):
            assert not await GuildCommandTree.interaction_check(None, interaction)
    asyncio.run(check())


def test_remote_json_and_cover_decoding_are_bounded():
    async def check():
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * 65536
                yield b"x" * 65536
                raise AssertionError("Response limit failed")
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=Stream()))) as client:
            with patch("app.http_limits.MAX_JSON_BYTES", 65536):
                try:
                    await bounded_request(client, "GET", "https://example.test")
                except ValueError:
                    pass
                else:
                    raise AssertionError("Oversized JSON accepted")
    asyncio.run(check())
    for dimensions, allowed in (((100, 200), True), ((4097, 1), False), ((3000, 3000), False)):
        output = io.BytesIO()
        Image.new("RGB", dimensions).save(output, format="PNG")
        assert (_poster_image(output.getvalue(), 40, 60, 4) is not None) == allowed


def test_rewards_only_assign_cosmetic_roles():
    from app.services.rewards import apply_rewards
    async def check():
        roles = {
            1: SimpleNamespace(name="cosmetic", managed=False, is_default=lambda: False,
                               permissions=SimpleNamespace(value=0)),
            2: SimpleNamespace(name="privileged", managed=False, is_default=lambda: False,
                               permissions=SimpleNamespace(value=8)),
        }
        member = SimpleNamespace(id=123, roles=[], guild=SimpleNamespace(get_role=roles.get),
                                 add_roles=AsyncMock())
        with patch("app.services.rewards._runtime", return_value={"reward_roles": {
                "easy": "1", "any_clear": "2"}}), patch(
                "app.services.rewards.completion_counts", return_value={}):
            assert await apply_rewards(member, tier="easy") == ["cosmetic"]
            member.add_roles.assert_awaited_once_with(roles[1], reason="Challenge clear (easy)")
    asyncio.run(check())


def test_link_modal_to_automatic_completion_achievement_and_cosmetic_reward():
    """Exercise the full application flow against synthetic Discord/Tadoku edges."""
    import discord
    from unittest.mock import MagicMock
    from app.bot import ChallengeBot
    from app.bot_commands import LinkModal
    from app.db import db_session, set_setting
    from app.services import achievements

    path = setup_tmp_db()
    try:
        async def check():
            uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            role = SimpleNamespace(name="Fixture cosmetic", managed=False,
                                   is_default=lambda: False,
                                   permissions=discord.Permissions.none())
            guild = SimpleNamespace(id=456, get_role=lambda rid: role if rid == 42 else None)
            member = MagicMock(spec=discord.Member)
            member.id, member.guild, member.roles = 333, guild, []
            member.add_roles = AsyncMock()
            interaction = SimpleNamespace(user=member, guild=guild,
                                          response=SimpleNamespace(send_message=AsyncMock()))
            modal = LinkModal()
            modal.user_id._value = uid
            modal.display_name._value = "Fixture member"
            await modal.on_submit(interaction)
            assert att.get_user("333")["tadoku_user_id"] == uid
            interaction.response.send_message.assert_awaited_once()
            assert att.start_attempt("333", tier="easy", number=4,
                                     param="Fixture Game", guild_id="456")["ok"]
            attempt_id = att.active_attempt("333")["id"]
            with db_session() as conn:
                set_setting(conn, "reward_roles_json", '{"easy":"42"}')
                set_setting(conn, "status_auto_complete", "true")
            bot = SimpleNamespace(get_user=lambda user_id: member,
                                  fetch_user=AsyncMock(return_value=member),
                                  get_guild=lambda guild_id: guild)
            logs = [{"id": "fixture", "amount": 40000, "unit": "characters",
                     "activity": "reading", "tags": ["vn"],
                     "description": "Fixture Game",
                     "created_at": datetime.now(timezone.utc).isoformat()}]
            with patch("app.tadoku.client.resolve_logs_for_user", new_callable=AsyncMock,
                       return_value=logs), patch("app.services.attempts.resolve_logs_for_user",
                                                new_callable=AsyncMock, return_value=logs), patch(
                       "app.bot.announce", new_callable=AsyncMock) as announce:
                assert await ChallengeBot._run_status_updates(bot) == 1
                assert await ChallengeBot._run_status_updates(bot) == 0
                announce.assert_awaited_once()
            assert not att.active_attempt("333")
            completed = att.list_attempts("333", status="completed")
            assert len(completed) == 1 and completed[0]["verify_method"] == "tadoku_poll"
            earned = [item for item in achievements.for_user("333") if item["earned"]]
            first_clear = next(item for item in earned if item["id"] == "achievement-001")
            assert first_clear["source_attempt_id"] == attempt_id
            member.add_roles.assert_awaited_once_with(role, reason="Challenge clear (easy)")
        asyncio.run(check())
    finally:
        cleanup_db(path)
