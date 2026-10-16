"""Corrections and rescoring compose from effective facts, not mutable rows."""
from datetime import datetime, timezone

from app.catalog import get_challenge
from app.db import db_session
from app.services import attempts, badges, lifecycle, scoring
from tests.helpers import setup_tmp_db, cleanup_db


def _completion():
    ch = get_challenge('tales-trails-2026')
    attempts.ensure_user('synthetic')
    start = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)
    with db_session() as conn:
        aid = conn.execute('INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at,completed_at,actual_completed_at) VALUES(?,?,?,?,?,?,?)',
                           ('synthetic', ch['id'], 'completed', start.isoformat(), lifecycle.calculate_deadline(ch, start).isoformat(),
                            '2026-02-10T12:00:00+00:00', '2026-02-10T12:00:00+00:00')).lastrowid
        lifecycle.capture_attempt(conn, aid, ch)
        scoring.sync_user(conn, 'synthetic')
        badges.sync_user(conn, 'synthetic')
    return aid, ch


def _recalculate(ch, policy, op):
    preview = scoring.preview_recalculation(policy, {'edition_ids': [ch['edition_id']]})
    return scoring.apply_recalculation(preview, actor='test', reason='Isolated test', operation_id=op)


def test_ordinary_edit_does_not_reclassify_rescoring():
    path = setup_tmp_db()
    try:
        aid, ch = _completion()
        with db_session() as conn:
            conn.execute("UPDATE challenge_defs SET tier='hard',number=909 WHERE id=?", (ch['id'],))
        _recalculate(ch, {'multipliers': {'monthly': 5}}, 'half')
        assert attempts.challenge_points('synthetic') == 5
        assert badges.duration_badges('synthetic')[0]['tier'] == 'easy'
    finally:
        cleanup_db(path)


def test_semantic_correction_is_used_by_later_recalculation():
    path = setup_tmp_db()
    try:
        aid, ch = _completion()
        preview = lifecycle.preview_correction(aid, {'tier': 'hard'})
        assert preview['impact']['points_delta'] == 20
        lifecycle.apply_correction(preview, actor='test', reason='Correct classification', operation_id='classify')
        assert attempts.challenge_points('synthetic') == 30
        _recalculate(ch, {'multipliers': {'monthly': 5}}, 'half')
        assert attempts.challenge_points('synthetic') == 15
        assert badges.duration_badges('synthetic')[0]['tier'] == 'hard'
    finally:
        cleanup_db(path)


def test_category_progress_uses_accepted_then_corrected_metadata():
    path = setup_tmp_db()
    try:
        aid, ch = _completion()
        with db_session() as conn:
            conn.execute("UPDATE challenge_defs SET category='physical' WHERE id=?", (ch['id'],))
        assert attempts.category_progress('synthetic')['ota']['points'] == 10
        assert attempts.category_progress('synthetic')['physical']['points'] == 0
        preview = lifecycle.preview_correction(aid, {'category': 'physical'})
        lifecycle.apply_correction(preview, actor='test', reason='Correct category', operation_id='category')
        assert attempts.category_progress('synthetic')['physical']['points'] == 10
        assert attempts.challenge_points('synthetic') == 10
    finally:
        cleanup_db(path)


def test_signed_total_is_not_clamped_differently_by_profile():
    path = setup_tmp_db()
    try:
        aid, _ = _completion()
        with db_session() as conn:
            scoring.set_award(conn, f'completion:{aid}', -5, operation_id='compensate', actor='test', reason='Signed compensating adjustment')
        assert attempts.challenge_points('synthetic') == -5
        assert attempts.leaderboard_standing('synthetic')['points'] == -5
        assert attempts.top_completers()[0]['points'] == -5
    finally:
        cleanup_db(path)


def test_effective_override_survives_classification_and_revert():
    path = setup_tmp_db()
    try:
        aid, ch = _completion()
        _recalculate(ch, {'points_override': 5}, 'override')
        preview = lifecycle.preview_correction(aid, {'tier': 'hard'})
        assert preview['impact']['points_delta'] == 0
        lifecycle.apply_correction(preview, actor='test', reason='Correct classification', operation_id='classify')
        assert attempts.challenge_points('synthetic') == 5
        _recalculate(ch, {'multipliers': {'monthly': 2}}, 'global')
        assert attempts.challenge_points('synthetic') == 5
        assert badges.duration_badges('synthetic')[0]['tier'] == 'hard'
        scoring.revert_operation('override', actor='test', reason='Compensate point operation only', revert_operation_id='revert')
        assert attempts.challenge_points('synthetic') == 10
        # A later recalculation must still use the corrected Hard classification.
        _recalculate(ch, {'multipliers': {'monthly': 2}}, 'refresh')
        assert attempts.challenge_points('synthetic') == 6
        with db_session() as conn:
            award = conn.execute('SELECT * FROM award_ledger WHERE attempt_id=?', (aid,)).fetchone()
            assert award['original_points'] == 10
    finally:
        cleanup_db(path)
