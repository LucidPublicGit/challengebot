"""Offline checks for exports, announcement lifecycle, and publishing safeguards."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.catalog import list_challenges
from app.catalog_announcements import (
    IMAGE_DIR, announcement_text, catalog_dump, text_chunks,
)
from app.bot_commands import challenge_announce, challenge_dump, challenge_group
from tests.helpers import cleanup_db, setup_tmp_db


def _interaction():
    return SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        guild=SimpleNamespace(id=1, filesize_limit=10 * 1024 * 1024),
        guild_id=1,
        client=object(),
    )


def test_catalog_export_and_change_text():
    path = setup_tmp_db()
    try:
        items = list_challenges()
        text = catalog_dump(items)
        assert all(f"ID: {c['id']}" in text and c["description"] in text for c in items)
        assert "".join(text_chunks(text)) == text
        for source in (text, "x" * 5000, "a\n" * 3000, "\n" + "x" * 2000):
            parts = text_chunks(source)
            assert "".join(parts) == source and max(map(len, parts)) <= 1900
        assert catalog_dump([]) == "No challenges found."
        physical = [c for c in items if c["category"] == "physical"]
        assert len(physical) == 8
        for change in ("new", "updated", "retired"):
            body = announcement_text(physical, change, "Announcement intro")
            assert body.startswith("Announcement intro")
            assert all(c["title"] in body for c in physical)
            assert ("/challenge start" in body) == (change != "retired")
        assert len(challenge_group.commands) <= 25
        assert (IMAGE_DIR / "physical-option-6.png").is_file()
    finally:
        cleanup_db(path)


def test_dump_and_announcements_offline():
    async def check():
        path = setup_tmp_db()
        try:
            target = SimpleNamespace(
                guild=SimpleNamespace(id=1), send=AsyncMock(), mention="#announcements"
            )
            interaction = _interaction()
            await challenge_dump.callback(interaction)
            calls = interaction.followup.send.call_args_list
            assert all("view" not in c.kwargs and "embed" not in c.kwargs for c in calls)
            assert all(c.kwargs["ephemeral"] for c in calls)
            assert calls[0].kwargs["file"].filename == "challenges.txt"
            assert "physical-walk-easy" in "".join(c.args[0] for c in calls[1:])
            with patch("app.bot_commands._admin_only", return_value=False):
                await challenge_announce.callback(_interaction(), publish=True, channel=target)
                target.send.assert_not_awaited()
            with patch("app.bot_commands._admin_only", return_value=True):
                preview = _interaction()
                await challenge_announce.callback(
                    preview, category="physical", image=6, channel=target
                )
                target.send.assert_not_awaited()
                assert all(c.kwargs["ephemeral"] for c in preview.followup.send.call_args_list)
                assert preview.followup.send.call_args_list[0].kwargs["file"].filename == "physical-option-6.png"
                body = "".join(c.args[0] for c in preview.followup.send.call_args_list[1:])
                assert "Walking Immersion Easy" in body and "Manga 3" not in body
                for change in ("new", "updated", "retired"):
                    await challenge_announce.callback(
                        _interaction(), change=change, message="A custom catalog change.",
                        publish=True, channel=target,
                    )
                assert target.send.await_count >= 6
                target.send.reset_mock()
                target.guild.id = 2
                await challenge_announce.callback(
                    _interaction(), message="Wrong server", publish=True, channel=target
                )
                target.send.assert_not_awaited()
                missing = _interaction()
                await challenge_announce.callback(missing, challenge_id="deleted", change="retired")
                assert "already deleted" in missing.followup.send.call_args.args[0]
        finally:
            cleanup_db(path)
    asyncio.run(check())
