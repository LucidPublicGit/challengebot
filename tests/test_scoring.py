"""Isolated ledger/migration tests: never open the configured runtime database."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db
from app.services import scoring


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'scoring.db')
        db.init_db(self.path)
        # Services may be under parallel development; tests own this isolated catalog.
        self.catalog_patch = patch.object(scoring, '_catalog_records', lambda conn: None)
        self.catalog_patch.start()
        with db.db_session() as conn:
            for i, group, points in [('tales', 'monthly', None), ('other', 'monthly', None), ('fixed', 'monthly', 20)]:
                conn.execute('''INSERT INTO challenge_defs
                    (id,year,tier,number,title,goal_kind,goal_amount,duration_group,points,edition_id,identity_id)
                    VALUES(?,2026,'easy',?,?,'minutes',1,?,?,?,?)''',
                    (i, {'tales': 1, 'other': 2, 'fixed': 3}[i], i, group, points, i, i))

    def tearDown(self):
        self.catalog_patch.stop()
        self.tmp.cleanup()

    def completion(self, challenge='tales', user='u', status='completed', snapshot=True):
        with db.db_session() as conn:
            ch = dict(conn.execute('SELECT * FROM challenge_defs WHERE id=?', (challenge,)).fetchone())
            snap = json.dumps(scoring.capture_snapshot(conn, ch)) if snapshot else None
            cur = conn.execute('''INSERT INTO attempts
                (discord_id,challenge_id,status,started_at,ends_at,completed_at,actual_completed_at,scoring_snapshot_json)
                VALUES(?,?,?,'2026-01-01T00:00:00+00:00','2026-02-01T00:00:00+00:00',
                       '2026-01-20T00:00:00+00:00','2026-01-19T00:00:00+00:00',?)''',
                (user, challenge, status, snap))
            scoring.ensure_completion_award(conn, cur.lastrowid)
            return cur.lastrowid

    def total(self, user='u'):
        with db.db_session() as conn:
            return scoring.user_points(conn, user)

    def apply(self, policy, scope=None, operation='change', **kwargs):
        preview = scoring.preview_recalculation(policy, scope, **kwargs)
        return scoring.apply_recalculation(preview, actor='admin', reason='test', operation_id=operation)

    def test_formula_and_overrides(self):
        for tier, base in scoring.BASE_POINTS.items():
            for group, multiplier in scoring.MULTIPLIERS.items():
                self.assertEqual(scoring.points_for_challenge({'tier': tier, 'duration_group': group}), base * multiplier)
        self.assertEqual(scoring.points_for_challenge({'points': 0}), 0)

    def test_10_to_5_idempotent_and_revert(self):
        self.completion()
        self.completion('other')
        preview = scoring.preview_recalculation({'points_override': 5}, {'edition_ids': ['tales']})
        self.assertEqual(preview['totals']['u'], {'old_points': 20, 'new_points': 15, 'delta': -5})
        result = scoring.apply_recalculation(preview, actor='a', reason='test', operation_id='once')
        self.assertEqual(result, scoring.apply_recalculation(preview, actor='a', reason='retry', operation_id='once'))
        self.assertEqual(self.total(), 15)
        with db.db_session() as conn:
            award = conn.execute("SELECT * FROM award_ledger WHERE source_key='completion:1'").fetchone()
            self.assertEqual((award['original_points'], award['effective_points']), (10, 5))
            self.assertEqual(conn.execute('SELECT delta FROM point_adjustments').fetchone()[0], -5)
        reverted = scoring.revert_operation('once', actor='a', reason='undo', revert_operation_id='undo')
        self.assertEqual(self.total(), 20)
        self.assertEqual(reverted, scoring.revert_operation('once', actor='a', reason='retry', revert_operation_id='undo'))
        with self.assertRaises(ValueError):
            scoring.revert_operation('once', actor='a', reason='again', revert_operation_id='another-undo')

    def test_stale_and_tampered_preview(self):
        self.completion()
        preview = scoring.preview_recalculation({'points_override': 5}, {'edition_ids': ['tales']})
        tampered = copy.deepcopy(preview)
        tampered['changes'][0]['new_points'] = 500
        with self.assertRaises(ValueError):
            scoring.apply_recalculation(tampered, actor='a', reason='bad', operation_id='bad')
        self.completion('other')
        with self.assertRaises(ValueError):
            scoring.apply_recalculation(preview, actor='a', reason='stale', operation_id='stale')
        self.assertEqual(self.total(), 20)

    def test_global_multiplier_excludes_override_and_retains_open(self):
        self.completion()
        self.completion('fixed')
        open_id = self.completion(status='active')
        self.apply({'multipliers': {'monthly': 5}})
        self.assertEqual(self.total(), 25)
        with db.db_session() as conn:
            snapshot = json.loads(conn.execute('SELECT scoring_snapshot_json FROM attempts WHERE id=?', (open_id,)).fetchone()[0])
            self.assertEqual(snapshot['points'], 10)
            conn.execute("UPDATE attempts SET status='completed' WHERE id=?", (open_id,))
        self.assertEqual(self.total(), 35)
        self.completion()
        self.assertEqual(self.total(), 40)

    def test_future_only_and_explicit_clear_override(self):
        self.completion('fixed')
        self.apply({'points_override': None}, {'edition_ids': ['fixed'], 'retroactive': False})
        self.assertEqual(self.total(), 20)
        self.completion('fixed')
        self.assertEqual(self.total(), 30)

    def test_include_open_and_scope(self):
        open_id = self.completion(status='active')
        self.apply({'multipliers': {'monthly': 7}}, {'edition_ids': ['tales']}, include_open=True)
        self.completion('other')
        with db.db_session() as conn:
            self.assertEqual(json.loads(conn.execute('SELECT scoring_snapshot_json FROM attempts WHERE id=?', (open_id,)).fetchone()[0])['points'], 7)
            ch = dict(conn.execute("SELECT * FROM challenge_defs WHERE id='tales'").fetchone())
            self.assertEqual(scoring.capture_snapshot(conn, ch)['points'], 7)
        self.assertEqual(self.total(), 10)

    def test_void_reinstate_preserves_recalculation(self):
        aid = self.completion()
        self.apply({'points_override': 5}, {'edition_ids': ['tales']})
        with db.db_session() as conn:
            conn.execute("UPDATE attempts SET status='void',correction_revision=1 WHERE id=?", (aid,))
            scoring.ensure_completion_award(conn, aid)
        self.assertEqual(self.total(), 0)
        with db.db_session() as conn:
            conn.execute("UPDATE attempts SET status='completed',correction_revision=2 WHERE id=?", (aid,))
        self.assertEqual(self.total(), 5)
        self.assertEqual(self.total(), 5)

    def test_signed_compensation_preserves_intervening_adjustment(self):
        aid = self.completion()
        self.apply({'points_override': 5}, {'edition_ids': ['tales']})
        with db.db_session() as conn:
            scoring.set_award(conn, f'completion:{aid}', 3, operation_id='manual', actor='a', reason='other correction')
        scoring.revert_operation('change', actor='a', reason='undo', revert_operation_id='undo')
        self.assertEqual(self.total(), 8)

    def test_atomic_rollback(self):
        self.completion()
        preview = scoring.preview_recalculation({'points_override': 5}, {'edition_ids': ['tales']})
        with patch.object(scoring, '_record', side_effect=RuntimeError('injected interruption')):
            with self.assertRaises(RuntimeError):
                scoring.apply_recalculation(preview, actor='a', reason='test', operation_id='interrupted')
        self.assertEqual(self.total(), 10)
        scoring.apply_recalculation(preview, actor='a', reason='retry', operation_id='interrupted')
        self.assertEqual(self.total(), 5)

    def test_date_filters_use_actual_completion_not_confirmation(self):
        aid = self.completion()
        with db.db_session() as conn:
            conn.execute('UPDATE attempts SET actual_completed_at=NULL WHERE id=?', (aid,))
        preview = scoring.preview_recalculation({'points_override': 5}, {'edition_ids': ['tales'], 'date_field': 'completed_at', 'date_from': '2026-01-01T00:00:00+00:00'})
        self.assertEqual(preview['changes'], [])

    def test_restart_and_repeated_legacy_completion_baseline(self):
        self.completion(snapshot=False)
        self.completion(snapshot=False)
        self.assertEqual(self.total(), 20)
        db.init_db(self.path)
        db.init_db(self.path)
        self.assertEqual(self.total(), 20)
        with db.db_session() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM award_ledger').fetchone()[0], 2)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_delayed_scheduled_start_reconstructs_override_cutovers(self):
        with patch.object(scoring, 'utcnow_iso', return_value='2026-01-20T00:00:00+00:00'):
            self.apply({'points_override': 5}, {'edition_ids': ['tales'], 'retroactive': False})
        with patch.object(scoring, 'utcnow_iso', return_value='2026-01-25T00:00:00+00:00'):
            scoring.revert_operation('change', actor='a', reason='undo', revert_operation_id='undo')
        with db.db_session() as conn:
            challenge = dict(conn.execute("SELECT * FROM challenge_defs WHERE id='tales'").fetchone())
            self.assertEqual(scoring.capture_snapshot(conn, challenge, at='2026-01-15T00:00:00+00:00')['points'], 10)
            self.assertEqual(scoring.capture_snapshot(conn, challenge, at='2026-01-22T00:00:00+00:00')['points'], 5)
            self.assertEqual(scoring.capture_snapshot(conn, challenge, at='2026-01-30T00:00:00+00:00')['points'], 10)

    def test_idempotency_key_collision_rejected(self):
        self.completion()
        self.apply({'points_override': 5}, {'edition_ids': ['tales']})
        preview = scoring.preview_recalculation({'points_override': 3}, {'edition_ids': ['tales']})
        with self.assertRaises(ValueError):
            scoring.apply_recalculation(preview, actor='a', reason='test', operation_id='change')


if __name__ == '__main__':
    unittest.main()
