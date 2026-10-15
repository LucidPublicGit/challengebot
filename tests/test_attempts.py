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


def test_category_progress_weights_points_and_deduplicates():
    from app.catalog import get_challenge

    path = setup_tmp_db()
    try:
        assert get_challenge("easy-1")["category"] == "ota"
        assert att.category_progress("nobody", year=1999)["ota"]["total"] == 0
        defs = [
            ("read", "ota", "manga", 1),
            ("listen", "ota", "shows", 3),
            ("normal", "ota", "any", 2),
            ("walk", "physical", "any", 4),
        ]
        for number, (cid, category, media, points) in enumerate(defs, 1):
            payload = dict(id=cid, year=2030, tier="special", number=number,
                           title=cid, goal_kind="minutes", goal_amount=1,
                           media_filter=media, points=points, category=category)
            assert att.admin_upsert_challenge(payload)["ok"]
        # Admin callers without the new field must preserve a physical assignment.
        payload.pop("category")
        assert att.admin_upsert_challenge(payload)["ok"]
        assert get_challenge("walk")["category"] == "physical"
        payload["category"] = "invalid"
        assert not att.admin_upsert_challenge(payload)["ok"]
        with db_session() as conn:
            for cid, status in [("read", "completed"), ("read", "completed"),
                                ("walk", "completed"), ("listen", "active")]:
                conn.execute(
                    "INSERT INTO attempts(discord_id, challenge_id, status, started_at, ends_at) "
                    "VALUES ('u', ?, ?, '2030-01-01', '2030-01-02')", (cid, status),
                )
        result = att.category_progress("u")
        ota = result["ota"]
        assert (ota["points"], ota["total_points"], ota["clears"], ota["total"]) == (1, 6, 1, 3)
        assert ota["breakdown"] == {"Reading": 1, "Listening": 0, "Normal": 0}
        assert result["physical"]["points"] == result["physical"]["total_points"] == 4
        # The row sources used by /userinfo include category labels.
        assert att.list_attempts("u", status="completed")[0]["category"] == "physical"
        assert att.open_attempts("u")[0]["category"] == "ota"
        assert att.category_progress("nobody")["ota"]["points"] == 0
    finally:
        cleanup_db(path)


def test_category_migration_and_yaml_import(tmp_path):
    from contextlib import closing
    import sqlite3
    from app.db import SCHEMA, init_db
    from app.catalog import get_challenge, upsert_challenges_from_yaml

    path = str(tmp_path / "old.db")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(SCHEMA.replace(
            "  category TEXT NOT NULL DEFAULT 'ota' CHECK(category IN ('ota', 'physical')),\n", ""
        ))
        conn.execute(
            "INSERT INTO challenge_defs(id, year, tier, number, title, goal_kind, goal_amount) "
            "VALUES ('old', 2026, 'easy', 1, 'Old challenge', 'minutes', 1)"
        )
    init_db(path)
    init_db(path)  # Additive migration is safe to rerun.
    assert get_challenge("old")["category"] == "ota"
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        "year: 2026\nchallenges:\n"
        "  - {id: walk, tier: special, number: 2, title: Walk, "
        "goal_kind: minutes, goal_amount: 10, points: 4, category: physical}\n",
        encoding="utf-8",
    )
    assert upsert_challenges_from_yaml(catalog) == 1
    assert get_challenge("walk")["category"] == "physical"
    assert att.category_progress("nobody")["physical"]["total_points"] == 4


def test_start_requires_tadoku_link():
    path = setup_tmp_db()
    try:
        r = att.start_attempt(
            "u1", tier="easy", number=4, param="VN", guild_id="g"
        )
        assert r["ok"] is False
        err = (r.get("error") or "").lower()
        assert "link" in err
        assert "/challenge link" in err
        assert "user_id:" not in err  # slash cmd is a modal, not a param
    finally:
        cleanup_db(path)


def test_start_wizard_tadoku_helpers():
    """Pure helpers that keep the start UI from dead-ending after link."""
    from unittest.mock import MagicMock

    from app import bot_commands as bc

    path = setup_tmp_db()
    try:
        assert bc._challenge_needs_tadoku({"verify": "tadoku"})
        assert not bc._challenge_needs_tadoku({"verify": "honor"})
        assert not bc._user_tadoku_linked("wiz")
        att.link_tadoku("wiz", "11111111-1111-1111-1111-111111111111")
        assert bc._user_tadoku_linked("wiz")
        v = MagicMock()
        v.message = "panel"
        assert bc._release_view(v) == "panel"
        assert v.message is None
        v.stop.assert_called_once()
        assert bc._release_view(None) is None
    finally:
        cleanup_db(path)


def test_start_requires_param_when_labeled():
    path = setup_tmp_db()
    try:
        r = att.start_attempt(
            "u1", challenge_id="hard-1", param=None, guild_id="g"
        )
        assert r["ok"] is False
        assert "book" in (r.get("error") or "").lower()
        ok = att.start_attempt(
            "u1", challenge_id="hard-1", param="Some Novel", guild_id="g"
        )
        assert ok["ok"] is True, ok
    finally:
        cleanup_db(path)


def test_fixed_param_one_piece_and_content_edit():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u_op", "11111111-1111-1111-1111-111111111111")
        r = att.start_attempt(
            "u_op", challenge_id="medium-7", param=None, guild_id="g"
        )
        assert r["ok"] is True, r
        assert r.get("param") == "One Piece"
        aid = int(r["attempt_id"])
        with db_session() as conn:
            conn.execute(
                "UPDATE attempts SET status='expired', content=? WHERE id=?",
                ("One Piece, Wrong Series", aid),
            )
        upd = att.user_update_attempt_content(
            "u_op", attempt_id=aid, remove="Wrong"
        )
        assert upd["ok"] is True
        assert upd["content"] == "One Piece"
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


def test_start_aggregate_skips_series():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u_agg", "11111111-1111-1111-1111-111111111111")
        r = att.start_attempt(
            "u_agg", challenge_id="easy-4", param=None, guild_id="g"
        )
        assert r["ok"] is True, r
        assert not r.get("param")
        hours = att.start_attempt(
            "u_h", challenge_id="hard-5", param=None, guild_id="g"
        )
        assert hours["ok"] is True, hours
        novel = att.start_attempt(
            "u_n", challenge_id="hard-1", param=None, guild_id="g"
        )
        assert novel["ok"] is False
        assert "book" in (novel.get("error") or "").lower()
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
        assert "already" in (r2.get("error") or "").lower() or "exclusive" in (
            r2.get("error") or ""
        ).lower()
        ab = att.abandon_attempt("u1")
        assert ab["ok"] is True
        assert ab.get("fail") is True
        assert att.active_attempt("u1") is None
        assert att.failure_count("u1") == 1
    finally:
        cleanup_db(path)


def test_scheduled_abandon_is_not_a_fail():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u_sched", "11111111-1111-1111-1111-111111111111")
        later = datetime.now(timezone.utc) + timedelta(days=2)
        r = att.start_attempt(
            "u_sched",
            tier="easy",
            number=4,
            param="Game",
            guild_id="g",
            starts_at=later,
        )
        assert r["ok"] is True and r.get("status") == "scheduled", r
        ab = att.abandon_attempt("u_sched")
        assert ab["ok"] is True
        assert ab.get("fail") is False
        assert att.failure_count("u_sched") == 0
    finally:
        cleanup_db(path)


def test_concurrent_long_with_exclusive():
    path = setup_tmp_db()
    try:
        from app.catalog import get_challenge

        att.link_tadoku("u_conc", "11111111-1111-1111-1111-111111111111")
        ch = get_challenge("medium-6")
        assert att.challenge_allows_concurrent(ch)

        # long window alone is not enough — flag required
        with db_session() as conn:
            conn.execute(
                "UPDATE challenge_defs SET allows_concurrent=0 WHERE id='medium-6'"
            )
        short = att.start_attempt(
            "u_conc", tier="easy", number=4, param="Game", guild_id="g"
        )
        assert short["ok"] is True, short
        blocked_long = att.start_attempt(
            "u_conc",
            challenge_id="medium-6",
            param="Dragon Ball",
            guild_id="g",
        )
        assert blocked_long["ok"] is False

        with db_session() as conn:
            conn.execute(
                "UPDATE challenge_defs SET allows_concurrent=1 WHERE id='medium-6'"
            )
        long = att.start_attempt(
            "u_conc",
            challenge_id="medium-6",
            param="Dragon Ball",
            guild_id="g",
        )
        assert long["ok"] is True, long
        assert len(att.active_attempts("u_conc")) == 2
        blocked = att.start_attempt(
            "u_conc", tier="easy", number=1, param="Series", guild_id="g"
        )
        assert blocked["ok"] is False
        ab = att.abandon_attempt("u_conc", attempt_id=int(short["attempt_id"]))
        assert ab["ok"] is True
        left = att.active_attempts("u_conc")
        assert len(left) == 1
        assert left[0]["challenge_id"] == "medium-6"
        conf = _run(
            att.confirm_attempt(
                "u_conc",
                attempt_id=int(long["attempt_id"]),
                note="finished DB",
            )
        )
        assert conf["ok"] is True, conf
        assert att.active_attempts("u_conc") == []
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


def test_honor_override_and_match_fixup():
    path = setup_tmp_db()
    try:
        att.link_tadoku("u_ho", "11111111-1111-1111-1111-111111111111")
        r = att.start_attempt(
            "u_ho", tier="easy", number=4, param="Game X", guild_id="g"
        )
        assert r["ok"]
        aid = int(r["attempt_id"])
        # save alias + fake log id for later confirms
        fix = att.user_update_attempt_content(
            "u_ho",
            attempt_id=aid,
            aliases="GameX, GX",
            log_ids="log-1, log-2",
        )
        assert fix["ok"]
        assert "GameX" in fix["aliases"]
        assert "log-1" in fix["log_ids"]

        # tadoku would fail without logs — honor override works
        conf = _run(
            att.confirm_attempt(
                "u_ho",
                attempt_id=aid,
                honor_override=True,
                content="Game X (manual)",
            )
        )
        assert conf["ok"] is True, conf
        assert conf.get("method") == "honor_override"
        assert conf.get("content") == "Game X (manual)"
    finally:
        cleanup_db(path)


def test_failoverride_disabled_setting():
    path = setup_tmp_db()
    try:
        from app.db import db_session, set_setting

        att.link_tadoku("u_fo", "11111111-1111-1111-1111-111111111111")
        r = att.start_attempt(
            "u_fo", tier="easy", number=4, param="Game", guild_id="g"
        )
        assert r["ok"]
        with db_session() as conn:
            set_setting(conn, "allow_honor_override", "false")
        conf = _run(
            att.confirm_attempt(
                "u_fo",
                attempt_id=int(r["attempt_id"]),
                honor_override=True,
                content="nope",
            )
        )
        assert conf["ok"] is False
        assert "disabled" in (conf.get("error") or "").lower()
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
        # default allow_reattempt false → one clear only
        blocked = att.start_attempt(
            "u9", tier="easy", number=4, param="G", guild_id="g"
        )
        assert blocked["ok"] is False
        assert "already" in (blocked.get("error") or "").lower()

        from app.db import db_session, set_setting

        with db_session() as conn:
            set_setting(conn, "allow_reattempt", "true")
        r = att.start_attempt(
            "u9", tier="easy", number=4, param="G", guild_id="g"
        )
        assert r["ok"] is True, r
        att.abandon_attempt("u9")
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


def test_completed_tiers_needs_every_challenge():
    path = setup_tmp_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        with db_session() as conn:
            for n in range(1, 4):
                conn.execute(
                    "INSERT INTO attempts("
                    "discord_id,challenge_id,status,started_at,ends_at)"
                    " VALUES(?,?,?,?,?)",
                    ("u", f"easy-{n}", "completed", now, now),
                )
        assert att.completed_tiers("u") == []
        assert att.highest_completed_tier("u") is None
        assert att.tier_slot_progress("u")["easy"] == [True, True, True, False, False, False]
        with db_session() as conn:
            conn.execute(
                "INSERT INTO attempts("
                "discord_id,challenge_id,status,started_at,ends_at)"
                " VALUES(?,?,?,?,?)",
                ("u", "easy-4", "completed", now, now),
            )
        assert att.completed_tiers("u") == []
        assert att.tier_slot_progress("u")["easy"] == [True, True, True, True, False, False]
        with db_session() as conn:
            for cid in ("physical-walk-easy", "physical-endurance-easy"):
                conn.execute(
                    "INSERT INTO attempts("
                    "discord_id,challenge_id,status,started_at,ends_at)"
                    " VALUES(?,?,?,?,?)",
                    ("u", cid, "completed", now, now),
                )
        assert att.completed_tiers("u") == ["easy"]
        assert att.highest_completed_tier("u") == "easy"
        assert att.tier_slot_progress("u")["easy"] == [True] * 6
        # extra completes of the same id don't invent other tiers
        with db_session() as conn:
            conn.execute(
                "INSERT INTO attempts("
                "discord_id,challenge_id,status,started_at,ends_at)"
                " VALUES(?,?,?,?,?)",
                ("u", "easy-1", "completed", now, now),
            )
        assert att.completed_tiers("u") == ["easy"]
        assert att.completed_tiers_for(["u", "nobody"])["nobody"] == []
    finally:
        cleanup_db(path)


def test_overflow_and_status_guard():
    from app.db import COUNTER_MAX, sql_int

    assert sql_int(2**80) == COUNTER_MAX
    assert sql_int(-3) == 0
    assert sql_int("no") == 0
    path = setup_tmp_db()
    try:
        assert att.failure_count("nobody") == 0
        bad = att.admin_update_attempt(1, status="'; DROP TABLE attempts;--")
        assert bad["ok"] is False
        huge = att.admin_update_attempt(2**80, status="void")
        assert huge["ok"] is False
        rows = att.list_attempts(limit=10**18)
        assert len(rows) <= 500
    finally:
        cleanup_db(path)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("test_attempts all ok")
