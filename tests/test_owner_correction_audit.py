import json
from datetime import datetime, timezone

from app import db
from app.catalog import get_challenge
from app.services import attempts, lifecycle
from tests.helpers import setup_tmp_db, cleanup_db
from tests.test_scoring_classification import _completion


def _selected_completion():
    ch = get_challenge('tales-trails-2026')
    attempts.ensure_user('synthetic')
    now = datetime.now(timezone.utc)
    with db.db_session() as conn:
        aid = conn.execute('INSERT INTO attempts(discord_id,challenge_id,param,status,started_at,ends_at) VALUES(?,?,?,?,?,?)',
                           ('synthetic', ch['id'], 'Selected game', 'active', now.isoformat(), lifecycle.calculate_deadline(ch, now).isoformat())).lastrowid
        lifecycle.capture_attempt(conn, aid, ch)
    result = attempts._complete(attempts.get_attempt(aid), method='honor', evidence={'honor': True}, note=None)
    assert result['ok']
    return aid


def test_owner_correction_preserves_original_work_and_award_and_invalidates_previews():
    path = setup_tmp_db()
    try:
        aid = _selected_completion()
        with db.db_session() as conn:
            before = dict(conn.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone())
        assert before['param'] == 'Selected game'
        preview = lifecycle.preview_correction(aid, {'note': 'Review prepared before owner correction'})
        original = before['original_completion_json']
        result = attempts.user_update_attempt_content('synthetic', attempt_id=aid, param='Corrected selected game', aliases='Alternative title', reason='Correct the selected game spelling')
        assert result['ok']
        with db.db_session() as conn:
            after = dict(conn.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone())
            event = dict(conn.execute("SELECT * FROM attempt_events WHERE attempt_id=? AND event_type='owner_content_correction'", (aid,)).fetchone())
            created = dict(conn.execute("SELECT * FROM attempt_events WHERE attempt_id=? AND event_type='created'", (aid,)).fetchone())
        assert json.loads(created['after_json'])['param'] == 'Selected game'
        assert event['actor'] == 'synthetic'
        assert event['reason'] == 'Correct the selected game spelling'
        assert json.loads(event['before_json'])['param'] == 'Selected game'
        assert json.loads(event['after_json'])['param'] == 'Corrected selected game'
        assert after['original_completion_json'] == original
        assert after['correction_revision'] == before['correction_revision'] + 1
        assert attempts.challenge_points('synthetic') == 10
        try:
            lifecycle.apply_correction(preview, actor='test', reason='Stale correction', operation_id='stale-owner')
            raise AssertionError('stale preview accepted')
        except ValueError as exc:
            assert 'stale' in str(exc).lower()
        # Exact retries are no-ops and owners cannot edit another member's row.
        attempts.user_update_attempt_content('synthetic', attempt_id=aid, param='Corrected selected game', aliases='Alternative title')
        assert not attempts.user_update_attempt_content('someone-else', attempt_id=aid, param='Forbidden')['ok']
        with db.db_session() as conn:
            assert conn.execute("SELECT COUNT(*) FROM attempt_events WHERE attempt_id=? AND event_type='owner_content_correction'", (aid,)).fetchone()[0] == 1
    finally:
        cleanup_db(path)


def test_reinstate_and_reclassify_use_distinct_linked_audited_adjustments():
    path = setup_tmp_db()
    try:
        aid, _ = _completion()
        void = lifecycle.preview_correction(aid, {'status': 'void'}, exception_reason='Administrative void')
        lifecycle.apply_correction(void, actor='reviewer', reason='Void wrong clear', operation_id='void-with-actor')
        preview = lifecycle.preview_correction(aid, {'status': 'completed', 'duration_group': 'yearly'})
        lifecycle.apply_correction(preview, actor='reviewer', reason='Reinstate with correct duration', operation_id='reinstate-classify')
        assert attempts.challenge_points('synthetic') == 50
        with db.db_session() as conn:
            changes = [dict(r) for r in conn.execute("SELECT * FROM point_adjustments WHERE source_key=? ORDER BY id", (f'completion:{aid}',))]
        assert [(r['old_points'], r['new_points']) for r in changes] == [(10, 0), (0, 10), (10, 50)]
        assert [r['operation_id'] for r in changes] == ['void-with-actor', 'reinstate-classify:reinstate', 'reinstate-classify']
        assert all(r['actor'] == 'reviewer' for r in changes)
    finally:
        cleanup_db(path)
