"""Attempt lifecycle + admin test harness (local DB, mocked Tadoku)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.db import db_session
from app.services import attempts as att
from tests.helpers import cleanup_db, setup_tmp_db


def _run(coro):
    return asyncio.run(coro)


def test_start_requires_tadoku_link():
    path = setup_tmp_db()
    try:
        r = att.start_attempt(
            "u1", tier="easy", number=4, param="VN", guild_id="g"
        )
        assert r["ok"] is False
        assert "Link" in (r.get("error") or "") or "link" in (r.get("error") or "").lower()
    finally:
        cleanup_db(path)


def test_start_requires_param_when_labeled():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u1", "11111111-1111-1111-1111-111111111111")
        r = att.start_attempt(
            "u1", tier="easy", number=4, param=None, guild_id="g"
        )
        assert r["ok"] is False
        assert "game" in (r.get("error") or "").lower() or "requires" in (
            r.get("error") or ""
        ).lower()
    finally:
        cleanup_db(path)


def test_start_by_challenge_id():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u1b", "11111111-1111-1111-1111-111111111111")
        r = att.start_attempt(
            "u1b", challenge_id="easy-4", param="Game", guild_id="g"
        )
        assert r["ok"] is True, r
        assert r["challenge"]["id"] == "easy-4"
        bad = att.start_attempt(
            "u1b", challenge_id="nope-x", param=None, guild_id="g"
        )
        assert bad["ok"] is False
    finally:
        cleanup_db(path)


def test_parse_schedule_local_offset():
    dt = att.parse_schedule_local("2026-06-01", "12:00", "+9")
    assert dt.tzinfo is not None
    # 12:00 UTC+9 → 03:00 UTC
    assert dt.hour == 3
    assert dt.day == 1


def test_schedule_then_activate():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u_sched", "11111111-1111-1111-1111-111111111111")
        future = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
        r = att.start_attempt(
            "u_sched",
            challenge_id="easy-4",
            param="Game",
            guild_id="g",
            starts_at=future,
        )
        assert r["ok"] is True, r
        assert r["status"] == "scheduled"
        assert att.active_attempt("u_sched") is None
        assert att.scheduled_attempt("u_sched") is not None
        # cannot confirm while scheduled
        conf = _run(att.confirm_attempt("u_sched"))
        assert conf["ok"] is False

        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(
            microsecond=0
        ).isoformat()
        with db_session() as conn:
            conn.execute(
                "UPDATE attempts SET started_at=? WHERE discord_id=? AND status='scheduled'",
                (past, "u_sched"),
            )
        activated, _ = att.tick_lifecycle()
        assert any(a["discord_id"] == "u_sched" for a in activated)
        assert att.active_attempt("u_sched") is not None
        assert att.scheduled_attempt("u_sched") is None
    finally:
        cleanup_db(path)


def test_one_active_and_abandon():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u1", "11111111-1111-1111-1111-111111111111")
        r = att.start_attempt(
            "u1", tier="easy", number=4, param="Game", guild_id="g"
        )
        assert r["ok"] is True
        r2 = att.start_attempt(
            "u1", tier="easy", number=1, param="Series", guild_id="g"
        )
        assert r2["ok"] is False
        assert "already" in (r2.get("error") or "").lower()
        ab = att.abandon_attempt("u1")
        assert ab["ok"] is True
        assert att.active_attempt("u1") is None
    finally:
        cleanup_db(path)


def test_honor_start_without_tadoku():
    path = setup_tmp_db()
    try:
        r = att.start_attempt(
            "u2", tier="hard", number=5, param=None, guild_id="g"
        )
        assert r["ok"] is True, r
        ch = r["challenge"]
        assert ch["verify"] == "honor"
    finally:
        cleanup_db(path)


def test_honor_confirm():
    path = setup_tmp_db()
    try:
        att.start_attempt("u3", tier="hard", number=5, param=None, guild_id="g")
        result = _run(att.confirm_attempt("u3", note="done 8h"))
        assert result["ok"] is True
        assert result.get("method") == "honor"
        assert att.active_attempt("u3") is None
    finally:
        cleanup_db(path)


def test_expire_overdue():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u4", "11111111-1111-1111-1111-111111111111")
        r = att.start_attempt(
            "u4", tier="easy", number=4, param="G", guild_id="g"
        )
        assert r["ok"]
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
            microsecond=0
        ).isoformat()
        with db_session() as conn:
            conn.execute(
                "UPDATE attempts SET ends_at=? WHERE discord_id=? AND status='active'",
                (past, "u4"),
            )
        expired = att.expire_overdue()
        assert len(expired) >= 1
        assert att.active_attempt("u4") is None
        rows = att.list_attempts(discord_id="u4", status="expired", limit=5)
        assert rows
    finally:
        cleanup_db(path)


def test_admin_fake_pass_dry_and_apply():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u5", "11111111-1111-1111-1111-111111111111")
        att.start_attempt(
            "u5", tier="easy", number=4, param="Game X", guild_id="g"
        )
        dry = _run(att.admin_test_verify("u5", mode="fake_pass", apply=False))
        assert dry.get("dry_run") is True
        assert dry.get("would_complete") is True
        assert att.active_attempt("u5") is not None

        fail = _run(att.admin_test_verify("u5", mode="fake_fail", apply=False))
        assert fail.get("ok") is False

        applied = _run(att.admin_test_verify("u5", mode="fake_pass", apply=True))
        assert applied.get("ok") is True
        assert applied.get("method") == "test_fake"
        assert att.active_attempt("u5") is None
        # Content stored from param / matched logs
        assert applied.get("content")
        done = att.list_attempts(discord_id="u5", status="completed", limit=1)
        assert done and att.display_content(done[0])
        counts = att.completion_counts("u5")
        assert counts.get("total", 0) >= 1
    finally:
        cleanup_db(path)


def test_admin_force_dry_run():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u6", "11111111-1111-1111-1111-111111111111")
        att.start_attempt(
            "u6", tier="easy", number=1, param="OP", guild_id="g"
        )
        dry = _run(att.admin_test_verify("u6", mode="force", apply=False))
        assert dry.get("dry_run") is True
        assert dry.get("would_complete") is True
        assert att.active_attempt("u6") is not None
    finally:
        cleanup_db(path)


def test_preview_real_with_mock_logs():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u7", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        att.start_attempt(
            "u7", tier="easy", number=4, param="Game X", guild_id="g"
        )
        fake = [
            {
                "id": "real-1",
                "amount": 40000,
                "unit": "characters",
                "activity": "reading",
                "tags": ["vn"],
                "description": "Game X route",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
        with patch(
            "app.services.attempts.resolve_logs_for_user",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            dry = _run(
                att.admin_test_verify("u7", mode="preview_real", apply=False)
            )
        assert dry.get("ok") is True
        assert dry.get("would_complete") is True
        assert dry.get("log_source") == "tadoku"
        assert dry.get("log_count") == 1
    finally:
        cleanup_db(path)


def test_preview_real_insufficient_logs():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u8", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        att.start_attempt(
            "u8", tier="easy", number=4, param="Game X", guild_id="g"
        )
        with patch(
            "app.services.attempts.resolve_logs_for_user",
            new_callable=AsyncMock,
            return_value=[],
        ):
            dry = _run(
                att.admin_test_verify("u8", mode="preview_real", apply=False)
            )
        assert dry.get("ok") is False
        assert dry.get("log_count") == 0
    finally:
        cleanup_db(path)


def test_reattempt_setting():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u9", "11111111-1111-1111-1111-111111111111")
        att.start_attempt(
            "u9", tier="easy", number=4, param="G", guild_id="g"
        )
        _run(att.admin_test_verify("u9", mode="fake_pass", apply=True))
        # default allow_reattempt true
        r = att.start_attempt(
            "u9", tier="easy", number=4, param="G", guild_id="g"
        )
        assert r["ok"] is True, r
        att.abandon_attempt("u9")

        from app.db import db_session, set_setting

        with db_session() as conn:
            set_setting(conn, "allow_reattempt", "false")
        # complete once more then block
        att.start_attempt(
            "u9", tier="easy", number=4, param="G", guild_id="g"
        )
        _run(att.admin_test_verify("u9", mode="fake_pass", apply=True))
        blocked = att.start_attempt(
            "u9", tier="easy", number=4, param="G", guild_id="g"
        )
        assert blocked["ok"] is False
        assert "re-attempt" in (blocked.get("error") or "").lower() or "already" in (
            blocked.get("error") or ""
        ).lower()
    finally:
        cleanup_db(path)


def test_admin_void():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u10", "11111111-1111-1111-1111-111111111111")
        r = att.start_attempt(
            "u10", tier="easy", number=4, param="G", guild_id="g"
        )
        aid = r["attempt_id"]
        _run(att.admin_test_verify("u10", mode="fake_pass", apply=True))
        out = att.admin_update_attempt(aid, status="void", verify_method="mod")
        assert out["ok"] is True
        rows = att.list_attempts(discord_id="u10", status="void", limit=5)
        assert any(x["id"] == aid for x in rows)
    finally:
        cleanup_db(path)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("test_attempts all ok")
