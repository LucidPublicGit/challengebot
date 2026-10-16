"""Historical sweep repairs: temp SQLite only, real services, no activity imports."""
import copy
import json
from unittest.mock import patch

import pytest

from app import catalog, db
from app.services import attempts, badges, lifecycle, rosters
from tests.helpers import setup_tmp_db, cleanup_db


@pytest.fixture
def fixture():
    path = setup_tmp_db()
    try:
        with db.db_session() as conn:
            row = dict(conn.execute("SELECT * FROM daily_rosters WHERE year=2026 AND tier='easy' ORDER BY version DESC LIMIT 1").fetchone())
        yield path, row, json.loads(row['requirements_json'])
    finally:
        cleanup_db(path)


def complete(member, editions):
    attempts.ensure_user(member)
    with db.db_session() as conn:
        for edition in editions:
            ch = dict(conn.execute('SELECT * FROM challenge_defs WHERE edition_id=?', (edition,)).fetchone())
            aid = conn.execute("INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at,completed_at,actual_completed_at) VALUES(?,?,'completed','2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00','2026-01-01T12:00:00+00:00','2026-01-01T12:00:00+00:00')", (member, ch['id'])).lastrowid
            lifecycle.capture_attempt(conn, aid, ch)
        from app.services import scoring
        scoring.sync_user(conn, member)
        badges.sync_user(conn, member)


def _apply(preview, operation='repair'):
    return rosters.apply_repair(preview, actor='test:admin', reason='Repair a historically wrong requirement set', operation_id=operation)


def _rows(table):
    with db.db_session() as conn:
        return [dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]


def test_preview_no_writes_apply_idempotent_original_receipts_preserved(fixture):
    path, old, required = fixture
    complete('full', required)
    complete('partial', required[:-1])
    before = {t: _rows(t) for t in ('daily_rosters', 'badge_receipts', 'award_ledger', 'badge_events', 'point_adjustments', 'admin_operations')}
    preview = rosters.preview_repair(old['id'], required[:-1])
    assert preview['impact']['partial']['points_delta'] == 2
    assert preview['impact']['full']['points_delta'] == 0
    assert preview['impact']['partial']['after']['badge']['status'] == 'active'
    assert {t: _rows(t) for t in before} == before  # no baseline/receipt/sequence leakage
    result = _apply(preview)
    assert _apply(preview) == result
    assert attempts.challenge_points('partial') == len(required) - 1 + 2
    assert attempts.challenge_points('full') == len(required) + 2
    with db.db_session() as conn:
        saved = conn.execute('SELECT * FROM daily_rosters WHERE id=?', (old['id'],)).fetchone()
        assert json.loads(saved['requirements_json']) == required
        replacement = conn.execute('SELECT * FROM daily_rosters WHERE id=?', (result['replacement_id'],)).fetchone()
        assert replacement['supersedes_roster_id'] == old['id']
        assert replacement['actor'] == 'test:admin'
        original_event = conn.execute("SELECT before_json FROM badge_events WHERE operation_id='repair' AND before_json IS NOT NULL").fetchone()
        assert json.loads(original_event['before_json'])['roster_id'] == old['id']
        issued = conn.execute("SELECT original_points,metadata_json FROM award_ledger WHERE source_key='daily_bonus:partial:2026:easy'").fetchone()
        assert issued['original_points'] == 2
        assert json.loads(issued['metadata_json'])['issued_operation_id'] == 'repair'
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    db.init_db(path)
    assert attempts.challenge_points('partial') == len(required) + 1
    assert len([r for r in _rows('award_ledger') if r['source_key'].startswith('daily_bonus:partial:2026:easy')]) == 1


def test_empty_repair_revokes_bonus_but_retains_receipt_and_points_audit(fixture):
    _, old, required = fixture
    complete('full', required)
    preview = rosters.preview_repair(old['id'], [])
    assert preview['warnings']
    assert preview['impact']['full']['points_delta'] == -2
    result = _apply(preview)
    assert result['point_delta'] == -2
    assert attempts.challenge_points('full') == len(required)
    assert badges.duration_badges('full', year=2026) == []
    history = badges.badge_history('full')
    assert history[0]['status'] == 'revoked'
    assert any(e['operation_id'] == 'repair' and e['event_type'] == 'revoked' for e in history[0]['events'])
    with db.db_session() as conn:
        delta = conn.execute("SELECT delta,actor FROM point_adjustments WHERE operation_id='repair'").fetchone()
        assert delta['delta'] == -2 and delta['actor'] == 'test:admin'


def test_historical_repair_does_not_become_current_roster_for_new_awards(fixture):
    _, old, required = fixture
    complete('full', required)
    created = attempts.admin_upsert_challenge(dict(id='extra-daily', year=2026, tier='easy', number=999,
        title='Extra Daily', goal_kind='pages', goal_amount=1, duration_group='daily', time_limit_hours=24))
    assert created['ok'], created
    # Current catalog roster now requires an extra edition; a repair to an older
    # version must not grant new awards against that older easier requirement.
    complete('new-member', required[:1])
    preview = rosters.preview_repair(old['id'], required[:1])
    _apply(preview)
    assert badges.duration_badges('new-member', year=2026) == []
    assert badges.duration_badges('full', year=2026)[0]['tier'] == 'easy'
    with db.db_session() as conn:
        badges.publish_rosters(conn)
        badges.sync_user(conn, 'new-member')
    assert badges.duration_badges('new-member', year=2026) == []


def test_repair_checks_alternative_published_rosters_before_revocation(fixture):
    _, old, required = fixture
    complete('full', required)
    lifecycle.retire_challenge(required[-1], actor='test', reason='Publish a smaller prospective roster', operation_id='retire')
    with db.db_session() as conn:
        alternate = conn.execute("SELECT id FROM daily_rosters WHERE year=2026 AND tier='easy' ORDER BY version DESC LIMIT 1").fetchone()[0]
    # Invalidating the original roster does not erase a valid alternative.
    preview = rosters.preview_repair(old['id'], [])
    _apply(preview)
    history = badges.badge_history('full')
    assert history[0]['status'] == 'active' and history[0]['roster_id'] == alternate
    assert attempts.challenge_points('full') == len(required) + 2


def test_repair_rejects_long_challenges_unknown_wrong_year_duplicates(fixture):
    _, old, required = fixture
    for invalid in (['tales-trails-2026'], ['unknown'], required + required[:1]):
        with pytest.raises(ValueError):
            rosters.preview_repair(old['id'], invalid)
    with pytest.raises(ValueError, match='unchanged'):
        rosters.preview_repair(old['id'], required)


def test_stale_tampered_and_operation_id_collisions(fixture):
    _, old, required = fixture
    complete('partial', required[:-1])
    preview = rosters.preview_repair(old['id'], required[:-1])
    changed = copy.deepcopy(preview)
    changed['impact']['partial']['points_delta'] = 999
    with pytest.raises(ValueError, match='stale or modified'):
        _apply(changed)
    with db.db_session() as conn:
        conn.execute("UPDATE attempts SET note='Concurrent correction' WHERE discord_id='partial'")
    with pytest.raises(ValueError, match='stale or modified'):
        _apply(preview)
    preview = rosters.preview_repair(old['id'], required[:-1])
    _apply(preview)
    with pytest.raises(ValueError, match='different request'):
        _apply(changed)


def test_interrupted_repair_rolls_back_every_dependency(fixture):
    _, old, required = fixture
    complete('full', required)
    preview = rosters.preview_repair(old['id'], [])
    before = {t: _rows(t) for t in ('daily_rosters', 'badge_receipts', 'award_ledger', 'point_adjustments', 'badge_events', 'admin_operations')}
    with patch.object(rosters, '_record_operation', side_effect=RuntimeError('Injected interruption')):
        with pytest.raises(RuntimeError):
            _apply(preview)
    assert {t: _rows(t) for t in before} == before
    _apply(preview)
    assert attempts.challenge_points('full') == len(required)
