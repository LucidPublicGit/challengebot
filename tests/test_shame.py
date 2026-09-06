"""Shame quotes + failure_count from expired attempts."""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.db import db_session, init_db, utcnow_iso
from app.services import attempts as att
from app.shame import format_shame_caption, pick_shame


def test_pick_shame_includes_count_when_multi():
    line = pick_shame("expired", 3)
    assert "3" in line
    line2 = pick_shame("expired", 5)
    assert "5" in line2


def test_format_multi_banner():
    text = format_shame_caption("expired", 3)
    assert "3 failures on record" in text
    assert text.startswith("📉")


def test_failure_count_from_expired():
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "t.db")
        init_db(db)
        now = utcnow_iso()
        with db_session() as conn:
            conn.execute(
                "INSERT INTO challenge_defs"
                "(id,year,tier,number,title,description,time_limit_hours,"
                "param_label,verify,goal_kind,goal_amount,media_filter)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "c1",
                    2025,
                    "easy",
                    1,
                    "T",
                    "",
                    24,
                    None,
                    "tadoku",
                    "pages",
                    10,
                    "any",
                ),
            )
            for status in ("expired", "expired", "completed", "abandoned"):
                conn.execute(
                    "INSERT INTO attempts"
                    "(discord_id,challenge_id,status,started_at,ends_at)"
                    " VALUES(?,?,?,?,?)",
                    ("u1", "c1", status, now, now),
                )
        assert att.failure_count("u1") == 2
        assert att.failure_count("nobody") == 0
