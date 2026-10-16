"""Classification corrections apply before completion without losing accepted facts."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import catalog, db
from app.services import attempts, badges, lifecycle, scoring
from tests.helpers import setup_tmp_db, cleanup_db


@pytest.mark.parametrize('status', ['active', 'scheduled'])
@pytest.mark.parametrize('override', [None, 7])
@pytest.mark.parametrize('changes, inherited', [({'duration_group': 'yearly'}, 50), ({'tier': 'medium'}, 20)])
def test_open_corrected_classification_resolves_effective_entitlement(status, override, changes, inherited):
    path = setup_tmp_db()
    try:
        attempts.ensure_user('synthetic')
        start = datetime.now(timezone.utc) - timedelta(hours=1)
        with db.db_session() as conn:
            conn.execute('UPDATE challenge_defs SET points=? WHERE id=?', (override, 'tales-trails-2026'))
            ch = dict(conn.execute('SELECT * FROM challenge_defs WHERE id=?', ('tales-trails-2026',)).fetchone())
            aid = conn.execute('INSERT INTO attempts(discord_id,challenge_id,param,status,started_at,ends_at) VALUES(?,?,?,?,?,?)',
                               ('synthetic', ch['id'], 'Synthetic selected game', status, start.isoformat(), lifecycle.calculate_deadline(ch, start).isoformat())).lastrowid
            lifecycle.capture_attempt(conn, aid, ch)
            original = dict(conn.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone())
        preview = lifecycle.preview_correction(aid, changes, recompute_deadline=True)
        lifecycle.apply_correction(preview, actor='classification-reviewer', reason='Correct the accepted classification', operation_id='open-classification')
        with db.db_session() as conn:
            corrected = dict(conn.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone())
        assert corrected['rules_snapshot_json'] == original['rules_snapshot_json']
        assert corrected['scoring_snapshot_json'] == original['scoring_snapshot_json']
        if status == 'scheduled':
            assert attempts.activate_due_scheduled()[0]['id'] == aid
        result = attempts._complete(attempts.get_attempt(aid), method='honor', evidence={'honor': True}, note=None)
        assert result['ok']
        expected = override if override is not None else inherited
        assert attempts.challenge_points('synthetic') == expected
        header = badges.duration_badges('synthetic')
        assert header[0]['glyph'] == ('年' if 'duration_group' in changes else '月')
        assert header[0]['tier'] == changes.get('tier', 'easy')
        with db.db_session() as conn:
            receipt = dict(conn.execute('SELECT * FROM award_ledger WHERE source_key=?', (f'completion:{aid}',)).fetchone())
            after = dict(conn.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone())
        assert receipt['original_points'] == expected  # First award, not a fictitious prior debit.
        audit = json.loads(receipt['metadata_json'])['classification_correction']
        assert audit['operation_id'] == 'open-classification'
        assert audit['actor'] == 'classification-reviewer'
        assert audit['reason'] == 'Correct the accepted classification'
        assert audit['effective_points'] == expected
        assert after['rules_snapshot_json'] == original['rules_snapshot_json']
        assert json.loads(original['scoring_snapshot_json'])['points'] == (override if override is not None else 10)
        assert attempts.category_progress('synthetic', year=2026)['ota']['points'] == expected
    finally:
        cleanup_db(path)


@pytest.mark.parametrize('status', ['active', 'scheduled'])
@pytest.mark.parametrize('multiplier, expected', [('monthly', 200), ('yearly', 28)])
def test_include_open_keeps_scoped_policy_and_effective_classification(status, multiplier, expected):
    path = setup_tmp_db()
    try:
        cid = 'tales-trails-2026'
        scope = {'edition_ids': [cid]}
        initial = scoring.preview_recalculation({'base_points': {'easy': 4}}, {**scope, 'retroactive': False})
        scoring.apply_recalculation(initial, actor='policy-admin', reason='Edition-scoped base', operation_id='scoped-base')
        attempts.ensure_user('synthetic')
        start = datetime.now(timezone.utc).replace(microsecond=0)
        ch = catalog.get_challenge(cid)
        with db.db_session() as conn:
            aid = conn.execute('INSERT INTO attempts(discord_id,challenge_id,param,status,started_at,ends_at) VALUES(?,?,?,?,?,?)',
                               ('synthetic', cid, 'Synthetic selected game', status, start.isoformat(), lifecycle.calculate_deadline(ch, start).isoformat())).lastrowid
            lifecycle.capture_attempt(conn, aid, ch)
        assert json.loads(attempts.get_attempt(aid)['scoring_snapshot_json'])['points'] == 40
        correction = lifecycle.preview_correction(aid, {'duration_group': 'yearly'}, recompute_deadline=True)
        lifecycle.apply_correction(correction, actor='reviewer', reason='Correct duration', operation_id='correct-scoped-duration')
        preview = scoring.preview_recalculation({'multipliers': {multiplier: 7}}, scope, include_open=True)
        snapshot = preview['open_attempts'][0]['new_snapshot']
        assert snapshot['policy']['base_points']['easy'] == 4
        assert snapshot['points'] == expected
        scoring.apply_recalculation(preview, actor='policy-admin', reason='Reviewed open entitlement', operation_id='include-open-scoped')
        if status == 'scheduled':
            attempts.activate_due_scheduled()
        assert attempts._complete(attempts.get_attempt(aid), method='honor', evidence={'honor': True}, note=None)['ok']
        assert attempts.challenge_points('synthetic') == expected
        assert badges.duration_badges('synthetic')[0]['glyph'] == '年'
    finally:
        cleanup_db(path)


def test_delayed_activation_retains_explicit_open_revision_and_can_revert():
    from unittest.mock import patch
    path = setup_tmp_db()
    try:
        cid = 'tales-trails-2026'
        scope = {'edition_ids': [cid]}
        now = datetime.now(timezone.utc)
        initial = scoring.preview_recalculation({'base_points': {'easy': 4}}, {**scope, 'retroactive': False})
        with patch.object(scoring, 'utcnow_iso', return_value=(now - timedelta(days=2)).isoformat()):
            scoring.apply_recalculation(initial, actor='policy-admin', reason='Earlier scoped base', operation_id='prior-base')
        attempts.ensure_user('synthetic')
        start = now - timedelta(hours=1)
        ch = catalog.get_challenge(cid)
        with db.db_session() as conn:
            aid = conn.execute('INSERT INTO attempts(discord_id,challenge_id,param,status,started_at,ends_at) VALUES(?,?,?,?,?,?)',
                ('synthetic', cid, 'Synthetic game', 'scheduled', start.isoformat(), lifecycle.calculate_deadline(ch, start).isoformat())).lastrowid
            lifecycle.capture_attempt(conn, aid, ch)
        correction = lifecycle.preview_correction(aid, {'duration_group': 'yearly'}, recompute_deadline=True)
        lifecycle.apply_correction(correction, actor='reviewer', reason='Correct duration', operation_id='delayed-duration')
        p = scoring.preview_recalculation({'multipliers': {'yearly': 7}}, scope, include_open=True)
        result = scoring.apply_recalculation(p, actor='policy-admin', reason='Explicit delayed recalculation', operation_id='delayed-open')
        assert result['open_snapshots'][str(aid)]['points'] == 28
        before = attempts.get_attempt(aid)['scoring_snapshot_json']
        attempts.activate_due_scheduled()
        assert attempts.get_attempt(aid)['scoring_snapshot_json'] == before
        assert json.loads(before)['policy_id'] == result['policy_id']
        reverted = scoring.revert_operation('delayed-open', actor='policy-admin', reason='Restore accepted policy', revert_operation_id='revert-delayed')
        assert reverted['reverts'] == 'delayed-open'
        assert attempts._complete(attempts.get_attempt(aid), method='honor', evidence={'honor': True}, note=None)['ok']
        assert attempts.challenge_points('synthetic') == 200
    finally:
        cleanup_db(path)
