"""Scoring review regressions using guarded, disposable SQLite databases only."""
import json

import pytest

from app import db
from app.services import scoring


@pytest.fixture(autouse=True)
def scoring_catalog(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / 'scoring-review.sqlite'))
    monkeypatch.setattr(scoring, '_catalog_records', lambda conn: None)
    monkeypatch.setattr(scoring, 'utcnow_iso', lambda: '2026-01-01T00:00:00+00:00')
    with db.db_session() as conn:
        conn.execute('''INSERT INTO scoring_policies(policy_json,recorded_at,actor,reason)
                        VALUES(?, '2026-01-01T00:00:00+00:00', 'fixture', 'Historical linear policy')''',
                     (json.dumps({'formula_version': 'linear_v1', 'multipliers': {'monthly': 10}}),))
        for number, (cid, identity, year) in enumerate([
            ('alpha', 'alpha', 2026), ('beta', 'beta', 2026), ('alpha-next', 'alpha', 2027),
        ], start=1):
            conn.execute('''INSERT INTO challenge_defs
                (id,year,tier,number,title,goal_kind,goal_amount,duration_group,edition_id,identity_id)
                VALUES(?,?,'easy',?,?,'minutes',1,'monthly',?,?)''',
                         (cid, year, number, cid, f'{cid}-edition', identity))


def _attempt(status='completed', challenge='alpha'):
    with db.db_session() as conn:
        ch = dict(conn.execute('SELECT * FROM challenge_defs WHERE id=?', (challenge,)).fetchone())
        snapshot = scoring.capture_snapshot(conn, ch)
        aid = conn.execute('''INSERT INTO attempts
            (discord_id,challenge_id,status,started_at,ends_at,completed_at,actual_completed_at,
             rules_snapshot_json,scoring_snapshot_json)
            VALUES('review-user',?,?,'2026-01-01T00:00:00+00:00','2026-02-01T00:00:00+00:00',
                   '2026-01-20T00:00:00+00:00','2026-01-20T00:00:00+00:00',?,?)''',
                           (challenge, status, json.dumps(ch), json.dumps(snapshot))).lastrowid
        scoring.ensure_completion_award(conn, aid)
        return aid


def _apply(policy, operation, scope=None, **kwargs):
    preview = scoring.preview_recalculation(policy, scope, **kwargs)
    return scoring.apply_recalculation(preview, actor='review', reason='Regression test', operation_id=operation)


def _revert(operation):
    return scoring.revert_operation(operation, actor='review', reason='Regression test',
                                    revert_operation_id=f'undo-{operation}')


def _snapshot(aid):
    with db.db_session() as conn:
        return json.loads(conn.execute('SELECT scoring_snapshot_json FROM attempts WHERE id=?', (aid,)).fetchone()[0])


def _award(aid):
    with db.db_session() as conn:
        return dict(conn.execute('SELECT * FROM award_ledger WHERE attempt_id=?', (aid,)).fetchone())


def _future_points(challenge='alpha'):
    with db.db_session() as conn:
        ch = dict(conn.execute('SELECT * FROM challenge_defs WHERE id=?', (challenge,)).fetchone())
        return scoring.capture_snapshot(conn, ch)['points']


@pytest.mark.parametrize('include_open', [False, True])
def test_future_override_preserves_historical_inheritance_in_preview_and_apply(include_open):
    inherited = {status: _attempt(status) for status in ('completed', 'active', 'scheduled')}
    _apply({'points_override': 7}, 'future-override',
           {'edition_ids': ['alpha-edition'], 'retroactive': False})
    overridden = {status: _attempt(status) for status in ('completed', 'active', 'scheduled')}

    preview = scoring.preview_recalculation({'multipliers': {'monthly': 5}}, include_open=include_open)
    assert [(c['source_key'], c['old_points'], c['new_points']) for c in preview['changes']] == [
        (f"completion:{inherited['completed']}", 10, 5),
    ]
    assert {c['attempt_id'] for c in preview['open_attempts']} == (
        {inherited['active'], inherited['scheduled']} if include_open else set())
    scoring.apply_recalculation(preview, actor='review', reason='Regression test', operation_id='monthly-five')

    assert _award(inherited['completed'])['effective_points'] == 5
    assert _award(overridden['completed'])['effective_points'] == 7
    for status in ('active', 'scheduled'):
        assert _snapshot(inherited[status])['points'] == (5 if include_open else 10)
        assert _snapshot(inherited[status])['points_override'] is None
        assert _snapshot(overridden[status])['points'] == 7
        assert _snapshot(overridden[status])['points_override'] == 7
    assert _future_points() == 7
    # The snapshot must still control the eventual completion.
    with db.db_session() as conn:
        for aid in (inherited['active'], overridden['active']):
            conn.execute("UPDATE attempts SET status='completed' WHERE id=?", (aid,))
            scoring.ensure_completion_award(conn, aid)
    assert _award(inherited['active'])['effective_points'] == (5 if include_open else 10)
    assert _award(overridden['active'])['effective_points'] == 7


def test_cleared_catalog_override_does_not_rescore_historical_overrides():
    _apply({'points_override': 7}, 'set-override', {'edition_ids': ['alpha-edition'], 'retroactive': False})
    overridden = {status: _attempt(status) for status in ('completed', 'active', 'scheduled')}
    _apply({'points_override': None}, 'clear-override', {'edition_ids': ['alpha-edition'], 'retroactive': False})
    inherited = {status: _attempt(status) for status in ('completed', 'active', 'scheduled')}
    _apply({'multipliers': {'monthly': 5}}, 'monthly-five', include_open=True)
    assert _award(overridden['completed'])['effective_points'] == 7
    assert _award(inherited['completed'])['effective_points'] == 5
    for status in ('active', 'scheduled'):
        assert _snapshot(overridden[status])['points'] == 7
        assert _snapshot(inherited[status])['points'] == 5
    assert _future_points() == 5


def test_revert_later_revision_then_earlier_restores_receipts_opens_and_defaults():
    completed = _attempt()
    opens = [_attempt(status) for status in ('active', 'scheduled')]
    originals = {aid: _snapshot(aid) for aid in opens}
    _apply({'multipliers': {'monthly': 5}}, 'A', include_open=True)
    _apply({'multipliers': {'monthly': 7}}, 'B', include_open=True)
    _revert('B')
    assert _award(completed)['effective_points'] == 5
    assert _future_points() == 5
    _revert('A')
    assert _award(completed)['effective_points'] == 10
    assert _future_points() == 10
    assert {aid: _snapshot(aid) for aid in opens} == originals


@pytest.mark.parametrize(('scope_a', 'scope_b'), [
    ({'edition_ids': ['alpha-edition']}, {'edition_ids': ['beta-edition']}),
    ({'identity_ids': ['alpha']}, {'identity_ids': ['beta']}),
    ({'years': [2026]}, {'years': [2027]}),
    ({'edition_ids': ['alpha-edition']}, {'identity_ids': ['beta']}),
    ({'edition_ids': ['alpha-edition']}, {'years': [2027]}),
    ({'identity_ids': ['beta']}, {'edition_ids': ['alpha-edition']}),
    ({'years': [2027]}, {'edition_ids': ['alpha-edition']}),
    ({'identity_ids': ['alpha'], 'years': [2026]}, {'edition_ids': ['alpha-next-edition']}),
])
def test_disjoint_formula_scopes_allow_revert(scope_a, scope_b):
    _apply({'multipliers': {'monthly': 5}}, 'A', scope_a)
    _apply({'multipliers': {'monthly': 7}}, 'B', scope_b)
    _revert('A')
    with db.db_session() as conn:
        for row in conn.execute('SELECT * FROM challenge_defs'):
            ch = dict(row)
            expected = 7 if scoring._scope_match(ch, scope_b) else 10
            assert scoring.capture_snapshot(conn, ch)['points'] == expected


@pytest.mark.parametrize(('scope_a', 'scope_b'), [
    ({}, {}),
    ({'edition_ids': ['alpha-edition']}, {'edition_ids': ['alpha-edition', 'beta-edition']}),
    ({'identity_ids': ['alpha']}, {'edition_ids': ['alpha-next-edition']}),
    ({'years': [2026]}, {'edition_ids': ['alpha-edition']}),
    ({'edition_ids': ['alpha-edition']}, {'identity_ids': ['alpha'], 'years': [2026]}),
    ({'identity_ids': ['alpha']}, {'years': [2028]}),
])
def test_effective_overlapping_formula_scopes_still_block_revert(scope_a, scope_b):
    # No receipts: the formula guard itself must reject the unsafe revert,
    # including scopes that can overlap in a future edition not yet cataloged.
    _apply({'multipliers': {'monthly': 5}}, 'A', scope_a)
    _apply({'multipliers': {'monthly': 7}}, 'B', scope_b)
    with pytest.raises(ValueError, match='policy changed since operation'):
        _revert('A')
    with db.db_session() as conn:
        assert not conn.execute("SELECT 1 FROM admin_operations WHERE operation_id='undo-A'").fetchone()


@pytest.mark.parametrize(('policy_a', 'policy_b'), [
    ({'multipliers': {'monthly': 5}}, {'formula_version': scoring.FORMULA_VERSION}),
    ({'formula_version': scoring.FORMULA_VERSION}, {'base_points': {'easy': 3}}),
])
def test_formula_version_conflicts_respect_withdrawal(policy_a, policy_b):
    _apply(policy_a, 'A', {'edition_ids': ['alpha-edition']})
    _apply(policy_b, 'B', {'identity_ids': ['alpha']})
    with pytest.raises(ValueError, match='policy changed since operation'):
        _revert('A')
    _revert('B')
    _revert('A')
    assert _future_points() == 10


def test_revert_decodes_original_result_once_for_all_receipts_and_opens(monkeypatch):
    completed = [_attempt() for _ in range(3)]
    opens = [_attempt(status) for status in ('active', 'scheduled') for _ in range(3)]
    originals = {aid: _snapshot(aid) for aid in opens}
    _apply({'multipliers': {'monthly': 5}}, 'scale-result', include_open=True)
    with db.db_session() as conn:
        encoded_result = conn.execute(
            "SELECT result_json FROM admin_operations WHERE operation_id='scale-result'"
        ).fetchone()[0]
    load = scoring._load
    result_decodes = 0

    def counted_load(value, *args, **kwargs):
        nonlocal result_decodes
        if value == encoded_result:
            result_decodes += 1
        return load(value, *args, **kwargs)

    monkeypatch.setattr(scoring, '_load', counted_load)
    _revert('scale-result')
    assert result_decodes == 1
    assert all(_award(aid)['effective_points'] == 10 for aid in completed)
    assert {aid: _snapshot(aid) for aid in opens} == originals
    assert _future_points() == 10


@pytest.mark.parametrize('missing', ['map', 'entry'])
def test_revert_legacy_open_result_falls_back_to_preview_snapshot(missing):
    _attempt()
    opens = [_attempt(status) for status in ('active', 'scheduled')]
    originals = {aid: _snapshot(aid) for aid in opens}
    _apply({'multipliers': {'monthly': 5}}, 'legacy-open', include_open=True)
    with db.db_session() as conn:
        operation = conn.execute(
            "SELECT * FROM admin_operations WHERE operation_id='legacy-open'"
        ).fetchone()
        preview, result = json.loads(operation['payload_json']), json.loads(operation['result_json'])
        missing_ids = opens if missing == 'map' else opens[:1]
        for entry in preview['open_attempts']:
            if entry['attempt_id'] in missing_ids:
                # Older operation formats kept the applied snapshot in the
                # preview itself instead of the result's separate snapshot map.
                entry['new_snapshot'] = result['open_snapshots'].pop(str(entry['attempt_id']))
        if missing == 'map':
            result.pop('open_snapshots')
        conn.execute('UPDATE admin_operations SET payload_json=?,result_json=? WHERE operation_id=?',
                     (scoring._json(preview), scoring._json(result), 'legacy-open'))
    _revert('legacy-open')
    assert {aid: _snapshot(aid) for aid in opens} == originals
    assert _future_points() == 10


@pytest.mark.parametrize(('conflict', 'error'), [
    ('open', 'open attempt changed'),
    ('policy', 'source scoring policy changed'),
    ('void', 'now-void source'),
])
def test_revert_conflict_rolls_back_all_compensation(conflict, error):
    completed = [_attempt() for _ in range(2)]
    opens = [_attempt(status) for status in ('active', 'scheduled')]
    _apply({'multipliers': {'monthly': 5}}, 'rollback-all', include_open=True)
    with db.db_session() as conn:
        if conflict == 'open':
            snapshot = _snapshot(opens[-1])
            snapshot['points'] = 99
            conn.execute('UPDATE attempts SET scoring_snapshot_json=? WHERE id=?',
                         (scoring._json(snapshot), opens[-1]))
        elif conflict == 'policy':
            conn.execute('UPDATE award_ledger SET current_policy_id=NULL WHERE attempt_id=?', (completed[-1],))
        else:
            metadata = json.loads(conn.execute(
                'SELECT metadata_json FROM award_ledger WHERE attempt_id=?', (completed[-1],)
            ).fetchone()[0])
            metadata['voided'] = True
            conn.execute('UPDATE award_ledger SET metadata_json=? WHERE attempt_id=?',
                         (scoring._json(metadata), completed[-1]))

    def persisted_state():
        with db.db_session() as conn:
            return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in ('attempts', 'award_ledger', 'point_adjustments', 'scoring_policies',
                                  'admin_operations', 'challenge_defs', 'meta')}

    before = persisted_state()
    with pytest.raises(ValueError, match=error):
        _revert('rollback-all')
    assert persisted_state() == before


def test_withdrawn_revision_does_not_hide_a_remaining_real_conflict():
    _apply({'multipliers': {'monthly': 5}}, 'A')
    _apply({'multipliers': {'monthly': 7}}, 'B', {'edition_ids': ['alpha-edition']})
    _apply({'multipliers': {'monthly': 9}}, 'C', {'edition_ids': ['beta-edition']})
    _revert('C')
    with pytest.raises(ValueError, match='policy changed since operation'):
        _revert('A')
    assert _future_points() == 7
    assert _future_points('beta') == 5
