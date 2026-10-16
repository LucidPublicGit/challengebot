"""Isolated badge/roster/ledger regressions; no runtime member data."""
import json
import sqlite3
import pytest

from app import db
from app.services import badges


@pytest.fixture
def conn():
    # Build only an isolated in-memory database; don't change the configured DB.
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(db.SCHEMA)
    db._migrate(c)
    db._migrate_lifecycle(c)
    yield c
    c.close()


def challenge(c, cid, *, tier="easy", duration="daily", year=2026, category="ota"):
    number = c.execute("SELECT COUNT(*) FROM challenge_defs").fetchone()[0] + 1
    c.execute("INSERT INTO challenge_defs(id,year,tier,number,title,goal_kind,goal_amount,category,duration_group,edition_id) VALUES (?,?,?,?,?,'minutes',1,?,?,?)",
              (cid, year, tier, number, cid, category, duration, f"edition:{cid}"))


def complete(c, cid, *, when="2026-05-01T12:00:00+00:00", user="u"):
    definition = dict(c.execute("SELECT * FROM challenge_defs WHERE id=?", (cid,)).fetchone())
    cur = c.execute("INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at,completed_at,actual_completed_at,edition_id,edition_year,rules_snapshot_json) VALUES (?,?,'completed','2026-01-01','2027-01-01',?,?,?,?,?)",
                    (user, cid, when, when, definition["edition_id"], definition["year"], json.dumps(definition)))
    return cur.lastrowid


def receipts(c):
    return [dict(r) for r in c.execute("SELECT * FROM badge_receipts ORDER BY id")]


def test_full_daily_roster_includes_physical_not_long_and_idempotent(conn):
    challenge(conn, "read")
    challenge(conn, "walk", category="physical")
    challenge(conn, "game", duration="monthly")
    assert len(badges.publish_rosters(conn)) == 1
    assert badges.publish_rosters(conn) == []
    complete(conn, "read")
    badges.sync_user(conn, "u")
    assert receipts(conn) == []
    complete(conn, "walk")
    badges.sync_user(conn, "u")
    badges.sync_user(conn, "u")
    assert len(receipts(conn)) == 1
    assert conn.execute("SELECT COUNT(*) FROM badge_events").fetchone()[0] == 1
    assert conn.execute("SELECT effective_points FROM award_ledger").fetchone()[0] == 2


def test_roster_edit_retirement_preserve_receipt_but_void_checks_alternative(conn):
    challenge(conn, "a")
    badges.publish_rosters(conn)
    a = complete(conn, "a")
    badges.sync_user(conn, "u")
    first = receipts(conn)[0]
    challenge(conn, "b")
    conn.execute("UPDATE challenge_defs SET lifecycle_state='retired' WHERE id='a'")
    badges.publish_rosters(conn)
    badges.sync_user(conn, "u")
    assert receipts(conn)[0]["roster_id"] == first["roster_id"]
    b = complete(conn, "b")
    conn.execute("UPDATE attempts SET status='void' WHERE id=?", (a,))
    badges.sync_user(conn, "u")
    assert receipts(conn)[0]["status"] == "active"
    assert receipts(conn)[0]["roster_id"] != first["roster_id"]
    assert conn.execute("SELECT COUNT(*) FROM award_ledger").fetchone()[0] == 1
    conn.execute("UPDATE attempts SET status='void' WHERE id=?", (b,))
    badges.sync_user(conn, "u")
    assert receipts(conn)[0]["status"] == "revoked"
    assert conn.execute("SELECT effective_points FROM award_ledger").fetchone()[0] == 0
    assert conn.execute("SELECT delta FROM point_adjustments").fetchone()[0] == -2
    conn.execute("UPDATE attempts SET status='completed' WHERE id=?", (a,))
    badges.sync_user(conn, "u")
    assert receipts(conn)[0]["status"] == "active"
    assert conn.execute("SELECT effective_points FROM award_ledger").fetchone()[0] == 2
    assert json.loads(conn.execute("SELECT after_json FROM badge_events ORDER BY id LIMIT 1").fetchone()[0])["roster_id"] == first["roster_id"]


def test_new_users_use_latest_only_and_empty_rosters_never_award(conn):
    challenge(conn, "a")
    badges.publish_rosters(conn)
    challenge(conn, "b")
    badges.publish_rosters(conn)
    complete(conn, "a")
    badges.sync_user(conn, "u")
    assert receipts(conn) == []
    conn.execute("UPDATE challenge_defs SET lifecycle_state='retired'")
    badges.publish_rosters(conn)
    badges.sync_user(conn, "u")
    assert receipts(conn) == []


@pytest.mark.parametrize('duration', ['weekly', 'monthly'])
def test_lifetime_alternatives_snapshot_classification_and_unknown_dates(conn, duration):
    challenge(conn, "m", duration=duration, tier="hard", year=2025)
    challenge(conn, "y", duration="yearly")
    a = complete(conn, "m", when="2025-01-01T00:00:00+00:00")
    b = complete(conn, "m", when="2026-01-01T00:00:00+00:00")
    complete(conn, "y", when=None)
    badges.sync_user(conn, "u")
    assert len(receipts(conn)) == 2
    conn.execute("UPDATE challenge_defs SET tier='easy',duration_group='daily',lifecycle_state='retired' WHERE id='m'")
    badges.sync_user(conn, "u")
    assert receipts(conn)[0]["tier"] == "hard"
    conn.execute("UPDATE attempts SET status='void' WHERE id=?", (a,))
    badges.sync_user(conn, "u")
    assert json.loads(receipts(conn)[0]["qualifying_attempts_json"]) == [b]
    assert receipts(conn)[1]["unlocked_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM award_ledger").fetchone()[0] == 0
    conn.execute("UPDATE attempts SET effective_completion_json=? WHERE id=?", (json.dumps({"tier": "medium"}), b))
    badges.sync_user(conn, "u")
    active = [r for r in receipts(conn) if r["status"] == "active"]
    assert {r["tier"] for r in active} == {"easy", "medium"}


def test_header_highest_max_four_year_scope_and_read_only(conn, monkeypatch):
    from contextlib import contextmanager
    @contextmanager
    def session():
        yield conn
    monkeypatch.setattr(badges, "db_session", session)
    for tier in badges.TIERS:
        for duration in badges.GLYPHS:
            cid = f"{tier}-{duration}"
            challenge(conn, cid, tier=tier, duration=duration)
            complete(conn, cid)
    badges.publish_rosters(conn)
    badges.sync_user(conn, "u")
    header = badges.duration_badges("u", 2026)
    assert [r["glyph"] for r in header] == ["日", "週", "月", "年"]
    assert all(r["tier"] == "ota_master" for r in header)
    assert conn.execute("SELECT effective_points FROM award_ledger WHERE source_key='daily_bonus:u:2026:ota_master'").fetchone()[0] == 8
    assert [r["glyph"] for r in badges.duration_badges("u", 2027)] == ["週", "月", "年"]
    history = badges.badge_history("u")
    assert len(history) == 16
    assert all(r["events"] for r in history)


def test_legacy_direct_insert_fallback_and_old_snapshot(conn):
    challenge(conn, "legacy", tier="ota_master")
    badges.publish_rosters(conn)
    conn.execute("INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at,completed_at) VALUES ('u','legacy','completed','2026-01-01','2026-01-02','2026-01-01')")
    badges.sync_user(conn, "u")
    assert receipts(conn)[0]["tier"] == "ota_master"
    baseline = conn.execute("SELECT challenge_snapshot_json,provenance FROM attempts").fetchone()
    assert json.loads(baseline[0])["duration_group"] == "daily"
    assert baseline[1] == "legacy_badge_baseline"
    assert receipts(conn)[0]["unlocked_at"] is None  # legacy confirmation is not actual completion
    assert conn.execute("SELECT effective_points FROM award_ledger").fetchone()[0] == 8
    # Freeze a pre-duration snapshot: future catalog edits cannot turn it Monthly.
    conn.execute("UPDATE attempts SET challenge_snapshot_json=?", (json.dumps({"tier": "ota_master", "year": 2026}),))
    conn.execute("UPDATE challenge_defs SET duration_group='monthly'")
    badges.publish_rosters(conn)
    badges.sync_user(conn, "u")
    assert len(receipts(conn)) == 1
    assert receipts(conn)[0]["status"] == "active"


def test_year_entitlements_future_publication_and_point_edits(conn):
    challenge(conn, "old", year=2025)
    challenge(conn, "current", year=2026)
    challenge(conn, "future", year=2026)
    conn.execute("UPDATE challenge_defs SET published_at='2999-01-01T00:00:00+00:00' WHERE id='future'")
    badges.publish_rosters(conn)
    complete(conn, "old", when="2026-01-02T00:00:00+00:00")
    complete(conn, "current")
    badges.sync_user(conn, "u")
    assert {r["year"] for r in receipts(conn)} == {2025, 2026}
    assert conn.execute("SELECT SUM(effective_points) FROM award_ledger").fetchone()[0] == 4
    conn.execute("UPDATE challenge_defs SET points=0")
    badges.sync_user(conn, "u")
    assert all(r["status"] == "active" for r in receipts(conn))
    assert conn.execute("SELECT COUNT(*) FROM badge_events").fetchone()[0] == 2


def test_caller_rollback_reverts_receipts_and_bonus(conn):
    challenge(conn, "a")
    badges.publish_rosters(conn)
    complete(conn, "a")
    conn.commit()
    conn.execute("SAVEPOINT correction")
    badges.sync_user(conn, "u")
    conn.execute("ROLLBACK TO correction")
    assert receipts(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM award_ledger").fetchone()[0] == 0
