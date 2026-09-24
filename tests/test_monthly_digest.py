"""Monthly digest schedule + classify (no Discord)."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings, merge_runtime
from app.db import db_session, init_db, utcnow_iso
from app.services import monthly_digest as md
from app.shame import empty_month_shame, new_year_mod_shame, new_year_welcome

# Fixed offset so Windows without tzdata still runs
TZ = timezone(timedelta(hours=-5))
md._runtime = lambda: merge_runtime(get_settings(), {})  # type: ignore[method-assign]


def _dt(y, m, d, h=12) -> datetime:
    return datetime(y, m, d, h, 0, 0, tzinfo=TZ)


def test_is_due_noon_first():
    assert md.is_due(_dt(2026, 4, 1, 11), last_key="2026-03") is False
    assert md.is_due(_dt(2026, 4, 1, 12), last_key="2026-03") is True
    assert md.is_due(_dt(2026, 4, 3, 9), last_key="2026-03") is True  # catch-up
    assert md.is_due(_dt(2026, 4, 1, 12), last_key="2026-04") is False


def test_classify_branches():
    assert md.classify(_dt(2026, 1, 1), completions=0, year_challenges=12) == "new_year_ready"
    assert md.classify(_dt(2026, 1, 1), completions=99, year_challenges=0) == "new_year_missing"
    assert md.classify(_dt(2026, 3, 1), completions=0, year_challenges=12) == "empty_shame"
    assert md.classify(_dt(2026, 3, 1), completions=3, year_challenges=12) == "leaderboard"


def test_captions():
    assert "March 2026" in empty_month_shame("March 2026")
    assert "2027" in new_year_welcome(2027, 10)
    assert "2027" in new_year_mod_shame(2027)


def test_top_completers_and_total():
    from app.services import attempts as att

    with tempfile.TemporaryDirectory() as td:
        init_db(str(Path(td) / "t.db"))
        now = utcnow_iso()
        with db_session() as conn:
            for cid, tier, num in (("c1", "easy", 1), ("c2", "ota_master", 1)):
                conn.execute(
                    "INSERT INTO challenge_defs"
                    "(id,year,tier,number,title,description,time_limit_hours,"
                    "param_label,verify,goal_kind,goal_amount,media_filter)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, 2026, tier, num, "T", "", 24, None, "tadoku", "pages", 10, "any"),
                )
            # u1: two easy (2 clears, 2 pts + easy sweep +2 = 4)
            # u2: one ota (1 clear, 4 pts + ota sweep +8 = 12) → leads
            for did, cid in (("u1", "c1"), ("u1", "c1"), ("u2", "c2")):
                conn.execute(
                    "INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at)"
                    " VALUES(?,?,?,?,?)",
                    (did, cid, "completed", now, now),
                )
            conn.execute(
                "INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at)"
                " VALUES(?,?,?,?,?)",
                ("u2", "c1", "expired", now, now),
            )
        assert att.total_completions() == 3
        top = att.top_completers(10)
        assert top[0]["discord_id"] == "u2"
        assert int(top[0]["clears"]) == 1 and int(top[0]["points"]) == 12
        assert int(top[0]["fails"]) == 1
        assert top[1]["discord_id"] == "u1"
        assert int(top[1]["clears"]) == 2 and int(top[1]["points"]) == 4
        assert att.challenge_points("u2") == 12
        assert att.challenge_points("u1") == 4
        st = att.leaderboard_standing("u2")
        assert st["rank"] == 1 and st["ranked"] == 2
        assert att.leaderboard_standing("u1")["rank"] == 2
        assert att.leaderboard_standing("nobody")["rank"] is None
