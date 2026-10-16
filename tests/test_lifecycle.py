"""Isolated lifecycle contracts; no live database or network."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from helpers import setup_tmp_db, cleanup_db
from app.db import db_session
from app.services import lifecycle as lc, scoring

UTC = timezone.utc

def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


class DeadlineTests(unittest.TestCase):
    def test_month_anniversaries_and_utc(self):
        for source, expected in (("2024-01-31T12:34", "2024-02-29T12:34"),
                                 ("2025-01-31T12:34", "2025-02-28T12:34"),
                                 ("2026-12-31T12:34", "2027-01-31T12:34"),
                                 ("2026-03-31T12:34", "2026-04-30T12:34")):
            self.assertEqual(lc.calculate_deadline({"duration_group": "monthly"}, dt(source)), dt(expected))
        self.assertEqual(lc.calculate_deadline({"duration_group": "yearly"}, dt("2024-02-29T13:00")),
                         dt("2025-02-28T13:00"))
        start = datetime.fromisoformat("2026-01-31T01:00:00+02:00")
        self.assertEqual(lc.calculate_deadline({"duration_group": "monthly"}, start), dt("2026-02-28T23:00"))

    def test_daily_validation_and_special_compatibility(self):
        for hours in (0, -1, 24.1, float("nan"), float("inf"), "bad"):
            with self.assertRaises(ValueError):
                lc.calculate_deadline({"duration_group": "daily", "time_limit_hours": hours}, dt("2026-01-01"))
        self.assertEqual(lc.calculate_deadline({"duration_group": "special", "time_limit_hours": 24 * 180},
                                              dt("2026-01-01")), dt("2026-06-30"))
        with self.assertRaises(ValueError):
            lc.calculate_deadline({}, datetime(2026, 1, 1))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.path = setup_tmp_db()
        with db_session() as conn:
            row = conn.execute("SELECT id FROM challenge_defs WHERE tier='easy' ORDER BY number LIMIT 1").fetchone()
            self.cid = row[0]
            conn.execute("UPDATE challenge_defs SET duration_group='monthly',repeat_policy='annual' WHERE id=?", (self.cid,))
            lc.ensure_catalog_records(conn)

    def tearDown(self):
        cleanup_db(self.path)

    def attempt(self, *, status="active", user="u", start="2026-01-31T10:00:00+00:00"):
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ch = dict(conn.execute("SELECT * FROM challenge_defs WHERE id=?", (self.cid,)).fetchone())
            end = lc.calculate_deadline(ch, datetime.fromisoformat(start)).isoformat()
            aid = conn.execute("INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at) VALUES(?,?,?,?,?)",
                               (user, self.cid, status, start, end)).lastrowid
            lc.capture_attempt(conn, aid, ch)
            return aid

    def read(self, aid):
        with db_session() as conn:
            return dict(conn.execute("SELECT * FROM attempts WHERE id=?", (aid,)).fetchone())

    def test_catalog_revision_idempotence_and_capture(self):
        aid = self.attempt(status="scheduled")
        original = self.read(aid)
        self.assertEqual(json.loads(original["scoring_snapshot_json"])["points"], 10)
        with db_session() as conn:
            count = conn.execute("SELECT COUNT(*) FROM definition_revisions").fetchone()[0]
            lc.ensure_catalog_records(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM definition_revisions").fetchone()[0], count)
            conn.execute("UPDATE challenge_defs SET title='Future title',points=99,duration_group='yearly' WHERE id=?", (self.cid,))
            lc.ensure_catalog_records(conn)
            lc.capture_attempt(conn, aid, {})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM definition_revisions").fetchone()[0], count + 1)
        current = self.read(aid)
        self.assertEqual(current["rules_snapshot_json"], original["rules_snapshot_json"])
        self.assertEqual(current["ends_at"], "2026-02-28T10:00:00+00:00")
        self.assertEqual(current["scheduled_start_original"], original["started_at"])
        merged = lc.effective_rules({**current, "title": "mutable", "duration_group": "daily"})
        self.assertEqual(merged["id"], aid)
        self.assertEqual(merged["duration_group"], "monthly")
        self.assertNotEqual(merged["title"], "mutable")

    def test_retire_restore_audited_and_idempotent(self):
        aid = self.attempt()
        result = lc.retire_challenge(self.cid, actor="admin", reason="retire", operation_id="retire")
        self.assertEqual(lc.retire_challenge(self.cid, actor="admin", reason="retire", operation_id="retire"), result)
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "retired"):
                lc.validate_start(conn, {"id": self.cid}, "other", dt("2026-12-31"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0], 1)
        self.assertEqual(self.read(aid)["status"], "active")
        lc.restore_challenge(self.cid, actor="admin", reason="restore")
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            lc.validate_start(conn, {"id": self.cid}, "other", dt("2026-12-31"))

    def test_identity_retirement_survives_catalog_refresh(self):
        lc.retire_challenge(self.cid, actor="a", reason="identity", identity=True)
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            lc.ensure_catalog_records(conn)
            with self.assertRaisesRegex(ValueError, "retired"):
                lc.validate_start(conn, {"id": self.cid}, "u", dt("2027-01-01"))
        lc.restore_challenge(self.cid, actor="a", reason="return", identity=True)

    def test_availability_half_open_and_future_retirement(self):
        with db_session() as conn:
            conn.execute("UPDATE challenge_defs SET available_from=?,available_until=? WHERE id=?",
                         ("2026-02-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00", self.cid))
            lc.ensure_catalog_records(conn)
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            lc.validate_start(conn, {"id": self.cid}, "u", dt("2026-02-01"))
            for start in ("2026-01-31", "2026-03-01"):
                with self.assertRaises(ValueError):
                    lc.validate_start(conn, {"id": self.cid}, "u", dt(start))
        lc.retire_challenge(self.cid, actor="a", reason="future", effective_at=dt("2030-01-01"))
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            lc.validate_start(conn, {"id": self.cid}, "u", dt("2026-02-01"))

    def test_open_identity_and_repeat_retry(self):
        aid = self.attempt()
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "already open"):
                lc.validate_start(conn, {"id": self.cid}, "u", dt("2026-03-01"))
            conn.execute("UPDATE attempts SET status='expired' WHERE id=?", (aid,))
            lc.validate_start(conn, {"id": self.cid}, "u", dt("2026-03-01"))
            conn.execute("UPDATE attempts SET status='completed' WHERE id=?", (aid,))
            with self.assertRaisesRegex(ValueError, "eligibility"):
                lc.validate_start(conn, {"id": self.cid}, "u", dt("2026-03-01"))

    def test_cross_edition_duplicate_and_annual_vs_once(self):
        aid = self.attempt()
        with db_session() as conn:
            ch = dict(conn.execute("SELECT * FROM challenge_defs WHERE id=?", (self.cid,)).fetchone())
            conn.execute("INSERT INTO challenge_defs(id,year,tier,number,title,goal_kind,goal_amount,identity_id,edition_id,duration_group,repeat_policy) "
                         "VALUES('renewal',2027,'easy',99,'Renewed','minutes',1,?,'renewal','monthly','annual')", (ch["identity_id"],))
            lc.ensure_catalog_records(conn)
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "already open"):
                lc.validate_start(conn, {"id": "renewal"}, "u", dt("2027-01-01"))
            conn.execute("UPDATE attempts SET status='completed' WHERE id=?", (aid,))
            lc.validate_start(conn, {"id": "renewal"}, "u", dt("2027-01-01"))
            conn.execute("UPDATE challenge_defs SET repeat_policy='once_ever' WHERE id='renewal'")
            with self.assertRaisesRegex(ValueError, "eligibility"):
                lc.validate_start(conn, {"id": "renewal"}, "u", dt("2027-01-01"))

    def test_daily_exclusive_but_long_concurrent(self):
        self.attempt()
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            daily = dict(conn.execute("SELECT * FROM challenge_defs WHERE duration_group='daily' AND tier='easy' LIMIT 1").fetchone())
            lc.validate_start(conn, daily, "u", dt("2026-02-01"))
            conn.execute("INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at) VALUES('u',?,'active',?,?)",
                         (daily["id"], "2026-02-01T00:00:00+00:00", "2026-02-02T00:00:00+00:00"))
            other = dict(conn.execute("SELECT * FROM challenge_defs WHERE duration_group='daily' AND id!=? LIMIT 1", (daily["id"],)).fetchone())
            with self.assertRaisesRegex(ValueError, "exclusive Daily"):
                lc.validate_start(conn, other, "u", dt("2026-02-01"))
            lc.validate_start(conn, {"id": self.cid}, "another-user", dt("2026-02-01"))

    def test_correction_preserves_original_and_recomputes_only_explicitly(self):
        aid = self.attempt()
        original = self.read(aid)
        preview = lc.preview_correction(aid, {"started_at": "2026-02-01T10:00:00+00:00"})
        self.assertEqual(preview["after"]["ends_at"], original["ends_at"])
        recomputed = lc.preview_correction(aid, {"started_at": "2026-02-01T10:00:00+00:00"}, recompute_deadline=True)
        self.assertEqual(recomputed["after"]["ends_at"], "2026-03-01T10:00:00+00:00")
        lc.apply_correction(recomputed, actor="a", reason="repair start", operation_id="repair")
        after = self.read(aid)
        self.assertEqual(after["original_deadline"], original["ends_at"])
        self.assertEqual(after["original_started_at"], original["started_at"])
        self.assertEqual(after["rules_snapshot_json"], original["rules_snapshot_json"])
        receipt = after["original_completion_json"]
        lc.apply_correction(lc.preview_correction(aid, {"note": "second correction"}), actor="a", reason="note", operation_id="second")
        self.assertEqual(self.read(aid)["original_completion_json"], receipt)

    def test_completion_void_reinstate_points_badges_and_idempotency(self):
        aid = self.attempt()
        p = lc.preview_correction(aid, {"status": "completed", "actual_completed_at": "2026-02-01T12:00:00+00:00",
                                       "confirmed_at": "2026-02-01T13:00:00+00:00"})
        result = lc.apply_correction(p, actor="admin", reason="confirm", operation_id="complete")
        self.assertEqual(lc.apply_correction(p, actor="admin", reason="confirm", operation_id="complete"), result)
        with db_session() as conn:
            self.assertEqual(scoring.user_points(conn, "u"), 10)
            self.assertTrue(conn.execute("SELECT 1 FROM badge_receipts WHERE discord_id='u' AND duration_group='monthly' AND status='active'").fetchone())
        lc.apply_correction(lc.preview_correction(aid, {"status": "void"}), actor="a", reason="void", operation_id="void")
        with db_session() as conn:
            self.assertEqual(scoring.user_points(conn, "u"), 0)
        lc.apply_correction(lc.preview_correction(aid, {"status": "completed"}), actor="a", reason="reinstate", operation_id="reinstate")
        with db_session() as conn:
            self.assertEqual(scoring.user_points(conn, "u"), 10)
            self.assertEqual(conn.execute("SELECT original_points FROM award_ledger WHERE attempt_id=?", (aid,)).fetchone()[0], 10)

    def test_stale_tampered_and_validation(self):
        aid = self.attempt()
        p = lc.preview_correction(aid, {"note": "correct"})
        with db_session() as conn:
            conn.execute("UPDATE challenge_defs SET title='new' WHERE id=?", (self.cid,))
        with self.assertRaisesRegex(ValueError, "stale"):
            lc.apply_correction(p, actor="a", reason="r", operation_id="stale")
        p = lc.preview_correction(aid, {"note": "correct"})
        p["after"]["status"] = "completed"
        with self.assertRaisesRegex(ValueError, "stale"):
            lc.apply_correction(p, actor="a", reason="r", operation_id="tamper")
        for changes in ({"actual_completed_at": "2026-02-28T10:00:00+00:00"},
                        {"started_at": "bad"}, {"started_at": "2026-01-01"}, {"status": "nonsense"}):
            with self.assertRaises(ValueError):
                lc.preview_correction(aid, changes)
        p = lc.preview_correction(aid, {"actual_completed_at": "2026-02-28T10:00:00+00:00"}, exception_reason="late attestation")
        self.assertTrue(p["warnings"])

    def test_sync_failure_rolls_back_all_changes(self):
        aid = self.attempt()
        before = self.read(aid)
        with db_session() as conn:
            events_before = [dict(r) for r in conn.execute('SELECT * FROM attempt_events WHERE attempt_id=?', (aid,))]
        p = lc.preview_correction(aid, {"status": "completed", "actual_completed_at": "2026-02-01T00:00:00+00:00"})
        with patch("app.services.badges.sync_user", side_effect=RuntimeError("projection failure")):
            with self.assertRaises(RuntimeError):
                lc.apply_correction(p, actor="a", reason="r", operation_id="rollback")
        self.assertEqual(self.read(aid), before)
        with db_session() as conn:
            self.assertEqual([dict(r) for r in conn.execute('SELECT * FROM attempt_events WHERE attempt_id=?', (aid,))], events_before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM award_ledger WHERE attempt_id=?", (aid,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
