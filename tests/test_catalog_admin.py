import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import catalog, db
from app.services import attempts, badges, catalog_admin, lifecycle, rosters
from tests.test_roster_repairs import fixture, complete, _rows


def payload(cid):
    ch = catalog.get_challenge(cid)
    fields = ('id', 'identity_id', 'edition_id', 'year', 'tier', 'number', 'title', 'description',
              'time_limit_hours', 'param_label', 'verify', 'goal_kind', 'goal_amount', 'media_filter',
              'allows_concurrent', 'fixed_param', 'points', 'category', 'duration_group', 'repeat_policy',
              'available_from', 'available_until', 'published_at')
    return {k: ch[k] for k in fields}


def apply(preview):
    return catalog_admin.apply_definition(preview, actor='test:admin', reason='Deliberate prospective revision', operation_id='catalog-edit')


def test_catalog_preview_includes_progress_and_preserves_earned_awards(fixture):
    _, old, required = fixture
    complete('full', required)
    complete('partial', required[:-1])
    data = {**payload(required[-1]), 'duration_group': 'monthly'}
    before = {t: _rows(t) for t in ('challenge_defs', 'definition_revisions', 'daily_rosters', 'badge_receipts', 'award_ledger', 'point_adjustments', 'admin_operations')}
    preview = catalog_admin.preview_definition(data)
    assert preview['roster_impact']['2026:easy']['partial']['points_delta'] == 2
    assert 'full' not in preview['roster_impact']['2026:easy']
    assert {t: _rows(t) for t in before} == before
    # The serialized preview has the same revalidation semantics as the object.
    preview = json.loads(json.dumps(preview))
    result = apply(preview)
    assert apply(preview) == result
    assert attempts.challenge_points('full') == len(required) + 2
    assert attempts.challenge_points('partial') == len(required) + 1
    assert badges.duration_badges('full', year=2026)[0]['glyph'] == '日'
    with db.db_session() as conn:
        revision = conn.execute('SELECT * FROM definition_revisions WHERE challenge_id=? ORDER BY id DESC LIMIT 1', (data['id'],)).fetchone()
        assert revision['actor'] == 'test:admin'
        assert revision['reason'] == 'Deliberate prospective revision'
        assert revision['operation_id'] == 'catalog-edit'
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_catalog_stale_tampered_atomic_rollback(fixture):
    _, old, required = fixture
    data = {**payload(required[-1]), 'duration_group': 'monthly'}
    preview = catalog_admin.preview_definition(data)
    changed = copy.deepcopy(preview)
    changed['after']['title'] = 'Forged'
    with pytest.raises(ValueError, match='stale or modified'):
        apply(changed)
    original = catalog.get_challenge(data['id'])
    with patch.object(catalog_admin, '_record_operation', side_effect=RuntimeError('Injected interruption')):
        with pytest.raises(RuntimeError):
            apply(preview)
    assert catalog.get_challenge(data['id']) == original
    apply(preview)
    stale = catalog_admin.preview_definition({**payload(required[-1]), 'title': 'Another title'})
    with db.db_session() as conn:
        conn.execute("UPDATE challenge_defs SET description='Concurrent revision' WHERE id='easy-1'")
    with pytest.raises(ValueError, match='stale or modified'):
        catalog_admin.apply_definition(stale, actor='test', reason='Concurrent edit', operation_id='another')


def test_retirement_preview_is_read_only_and_matches_committed_progress(fixture):
    _, old, required = fixture
    complete('partial', required[:-1])
    snapshot = {t: _rows(t) for t in ('challenge_defs', 'daily_rosters', 'badge_receipts', 'award_ledger', 'lifecycle_events', 'admin_operations')}
    preview = catalog_admin.preview_lifecycle(required[-1], action='retire', reason='Retire one prospective requirement')
    assert preview['progress_before']['2026:easy']['partial']['points'] == len(required) - 1
    assert preview['progress_after_now']['2026:easy']['partial']['points'] == len(required) + 1
    assert {t: _rows(t) for t in snapshot} == snapshot
    result = catalog_admin.apply_lifecycle(preview, actor='test', reason='Retire one prospective requirement', operation_id='lifecycle-edit')
    assert catalog_admin.apply_lifecycle(preview, actor='test', reason='Retry', operation_id='lifecycle-edit') == result
    assert attempts.challenge_points('partial') == preview['progress_after_now']['2026:easy']['partial']['points']
    assert badges.duration_badges('partial', year=2026)[0]['glyph'] == '日'


def test_future_retirement_has_separate_forecast_and_never_grants_early(fixture):
    _, old, required = fixture
    complete('partial', required[:-1])
    future = datetime.now(timezone.utc) + timedelta(days=2)
    preview = catalog_admin.preview_lifecycle(required[-1], action='retire', effective_at=future, reason='Future boundary')
    assert preview['progress_after_now']['2026:easy']['partial']['points'] == len(required) - 1
    assert preview['progress_at_effective_time_forecast']['2026:easy']['partial']['points'] == len(required) + 1
    catalog_admin.apply_lifecycle(preview, actor='test', reason='Future boundary', operation_id='future-edit')
    assert attempts.challenge_points('partial') == len(required) - 1
    assert badges.duration_badges('partial', year=2026) == []


@pytest.mark.parametrize('change', ['add', 'retire'])
def test_historical_repair_then_catalog_change_allocates_unique_version(fixture, change):
    _, old, required = fixture
    repair = rosters.preview_repair(old['id'], required[:-1])
    rosters.apply_repair(repair, actor='test', reason='Repair historical requirement', operation_id='repair-before-edit')
    if change == 'add':
        data = {**payload(required[0]), 'id': 'new-reviewed-daily', 'identity_id': 'new-reviewed-daily',
                'edition_id': 'new-reviewed-daily', 'number': 999}
        apply(catalog_admin.preview_definition(data))
    else:
        preview = catalog_admin.preview_lifecycle(required[-1], action='retire', reason='Retire after repair')
        catalog_admin.apply_lifecycle(preview, actor='test', reason='Retire after repair', operation_id='retire-after-repair')
    with db.db_session() as conn:
        versions = [dict(r) for r in conn.execute("SELECT * FROM daily_rosters WHERE year=2026 AND tier='easy' ORDER BY version")]
    assert [r['version'] for r in versions] == [old['version'], old['version'] + 1, old['version'] + 2]
    assert versions[1]['supersedes_roster_id'] == old['id']
    assert versions[2]['supersedes_roster_id'] is None


def test_repair_then_scheduled_retirement_boundary_and_restart(fixture):
    path, old, required = fixture
    rosters.apply_repair(rosters.preview_repair(old['id'], required[:-1]), actor='test', reason='Repair old roster', operation_id='repair-before-boundary')
    future = datetime.now(timezone.utc) + timedelta(days=2)
    lifecycle.retire_challenge(required[-1], actor='test', reason='Later retirement', effective_at=future, operation_id='later-retirement')
    class BoundaryClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future
    with patch.object(badges, 'datetime', BoundaryClock):
        db.init_db(path)
        db.init_db(path)
    with db.db_session() as conn:
        versions = [dict(r) for r in conn.execute("SELECT * FROM daily_rosters WHERE year=2026 AND tier='easy' ORDER BY version")]
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert [r['version'] for r in versions] == [old['version'], old['version'] + 1, old['version'] + 2]
    assert json.loads(versions[-1]['requirements_json']) == sorted(required[:-1])


def test_catalog_rejects_implicit_override_clear_nan_invalid_identity(fixture):
    _, _, required = fixture
    data = payload('tales-trails-2026')
    with db.db_session() as conn:
        conn.execute("UPDATE challenge_defs SET points=5 WHERE id='tales-trails-2026'")
    with pytest.raises(ValueError, match='scoring preview'):
        catalog_admin.preview_definition(data)
    data = payload(required[0])
    for invalid in ({'goal_amount': float('nan')}, {'year': True}, {'identity_id': 'unrelated'}, {'available_from': '2027-01-01T00:00:00Z', 'available_until': '2026-01-01T00:00:00Z'}):
        with pytest.raises(ValueError):
            catalog_admin.preview_definition({**data, **invalid})
