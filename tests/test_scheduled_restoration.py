from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import catalog, db
from app.services import attempts, lifecycle
from tests.helpers import setup_tmp_db, cleanup_db


@pytest.fixture
def fixture():
    path = setup_tmp_db()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        yield path, now, now + timedelta(days=2)
    finally:
        cleanup_db(path)


def validate(when, cid='easy-1'):
    with db.db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        lifecycle.validate_start(conn, catalog.get_challenge(cid), 'new-synthetic', when)


def test_edition_restoration_boundary_restart_and_no_deadline_changes(fixture):
    path, now, restore = fixture
    attempts.link_tadoku('synthetic', 'synthetic-provider-id')
    started = attempts.start_attempt('synthetic', challenge_id='easy-1', param=None, guild_id='synthetic')
    assert started['ok']
    with db.db_session() as conn:
        saved = dict(conn.execute('SELECT * FROM attempts WHERE id=?', (started['attempt_id'],)).fetchone())
    lifecycle.retire_challenge('easy-1', actor='test', reason='Retire edition', operation_id='retire')
    result = lifecycle.restore_challenge('easy-1', actor='test', reason='Reopen next window', effective_at=restore, operation_id='restore')
    assert lifecycle.restore_challenge('easy-1', actor='test', reason='Retry', effective_at=restore, operation_id='restore') == result
    assert catalog.get_challenge('easy-1')['lifecycle_state'] == 'retired'
    assert 'easy-1' not in {c['id'] for c in catalog.list_challenges()}
    with pytest.raises(ValueError, match='retired'):
        validate(restore - timedelta(seconds=1))
    validate(restore)
    db.init_db(path)  # Restart before the boundary must not reopen early.
    assert catalog.get_challenge('easy-1')['lifecycle_state'] == 'retired'
    with patch('app.db.utcnow_iso', return_value=(restore + timedelta(seconds=1)).isoformat()), patch('app.services.lifecycle.utcnow_iso', return_value=(restore + timedelta(seconds=1)).isoformat()):
        db.init_db(path)
    assert catalog.get_challenge('easy-1')['lifecycle_state'] == 'active'
    assert 'easy-1' in {c['id'] for c in catalog.list_challenges()}
    with db.db_session() as conn:
        current = dict(conn.execute('SELECT * FROM attempts WHERE id=?', (started['attempt_id'],)).fetchone())
        assert current['ends_at'] == saved['ends_at']
        assert current['original_deadline'] == saved['original_deadline']
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_identity_schedule_cancel_and_explicit_conflict(fixture):
    _, now, restore = fixture
    lifecycle.retire_challenge('easy-1', actor='test', reason='Permanent retirement', identity=True, operation_id='retire')
    lifecycle.restore_challenge('easy-1', actor='test', reason='Schedule identity reopening', identity=True, effective_at=restore, operation_id='restore')
    with pytest.raises(ValueError, match='retired'):
        validate(restore - timedelta(seconds=1))
    validate(restore)
    with pytest.raises(ValueError, match='cancel it explicitly'):
        lifecycle.retire_challenge('easy-1', actor='test', reason='Conflicting retirement', identity=True, operation_id='conflict')
    result = lifecycle.cancel_restoration('easy-1', actor='test', reason='Cancel scheduled reopening', identity=True, operation_id='cancel')
    assert result == lifecycle.cancel_restoration('easy-1', actor='test', reason='Retry', identity=True, operation_id='cancel')
    with pytest.raises(ValueError, match='retired'):
        validate(restore + timedelta(days=1))
    with db.db_session() as conn:
        event = conn.execute("SELECT * FROM lifecycle_events WHERE event_type='restoration_cancelled'").fetchone()
        assert event['actor'] == 'test' and event['operation_id'] == 'cancel'
    with pytest.raises(ValueError, match='No pending'):
        lifecycle.cancel_restoration('easy-1', actor='test', reason='Already cancelled', identity=True, operation_id='cancel-again')


@pytest.mark.parametrize('identity', [False, True])
def test_progress_denominators_follow_retirement_and_restoration_instants(fixture, identity):
    _, now, retired = fixture
    restored = retired + timedelta(days=1)
    sql_filter = catalog.availability_sql
    def projection(at):
        with patch.object(catalog, 'availability_sql', side_effect=lambda alias='challenge_defs': sql_filter(alias, at=at)):
            return (attempts.tier_slot_progress('no-clears', year=2026),
                    attempts.category_progress('no-clears', year=2026),
                    {c['id'] for c in catalog.list_challenges(year=2026)})
    baseline_slots, baseline_category, _ = projection(now)
    lifecycle.retire_challenge('easy-1', actor='test', reason='Scheduled cutover', identity=identity, effective_at=retired, operation_id='progress-retire')
    lifecycle.restore_challenge('easy-1', actor='test', reason='Scheduled return', identity=identity, effective_at=restored, operation_id='progress-restore')
    for at, available in ((now, True), (retired - timedelta(seconds=1), True), (retired, False),
                          (retired + timedelta(seconds=1), False), (restored - timedelta(seconds=1), False),
                          (restored, True), (restored + timedelta(seconds=1), True)):
        slots, category, listed = projection(at)
        missing = int(not available)
        assert ('easy-1' in listed) is available
        assert len(slots['easy']) == len(baseline_slots['easy']) - missing
        assert category['ota']['total'] == baseline_category['ota']['total'] - missing
        assert category['ota']['total_points'] == baseline_category['ota']['total_points'] - missing


def test_restoration_must_follow_scheduled_retirement(fixture):
    _, now, restore = fixture
    with pytest.raises(ValueError, match='Schedule a retirement'):
        lifecycle.restore_challenge('easy-1', actor='test', reason='Not retired', effective_at=restore, operation_id='not-retired')
    lifecycle.retire_challenge('easy-1', actor='test', reason='Future retirement', effective_at=restore, operation_id='retire')
    with pytest.raises(ValueError, match='after retirement'):
        lifecycle.restore_challenge('easy-1', actor='test', reason='Wrong order', effective_at=restore, operation_id='bad-order')
    lifecycle.restore_challenge('easy-1', actor='test', reason='Bounded retirement interval', effective_at=restore + timedelta(days=1), operation_id='restore')
    validate(restore - timedelta(seconds=1))
    with pytest.raises(ValueError, match='retired'):
        validate(restore)
    validate(restore + timedelta(days=1))
