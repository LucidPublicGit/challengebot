"""Monthly digest schedule + classify (no Discord)."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import db_session, init_db, utcnow_iso
from app.services import monthly_digest as md
from app.shame import empty_month_shame, new_year_mod_shame, new_year_welcome

# Fixed offset so Windows without tzdata still runs
TZ = timezone(timedelta(hours=-5))


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
            conn.execute(
                "INSERT INTO challenge_defs"
                "(id,year,tier,number,title,description,time_limit_hours,"
                "param_label,verify,goal_kind,goal_amount,media_filter)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("c1", 2026, "easy", 1, "T", "", 24, None, "tadoku", "pages", 10, "any"),
            )
            for did, st in (("u1", "completed"), ("u1", "completed"), ("u2", "completed")):
                conn.execute(
                    "INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at)"
                    " VALUES(?,?,?,?,?)",
                    (did, "c1", st, now, now),
                )
        assert att.total_completions() == 3
        top = att.top_completers(10)
        assert top[0]["discord_id"] == "u1" and int(top[0]["n"]) == 2
