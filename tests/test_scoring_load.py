"""Opt-in synthetic scoring scale check; conftest enforces disposable SQLite.

CHALLENGEBOT_LOAD_MEMBERS=200 python -m pytest -o addopts='' -q -s tests/test_scoring_load.py
Timings are observations, not portable latency guarantees or live acceptance.
"""
import json
import os
from time import perf_counter

import pytest

from app import db
from app.services import scoring


@pytest.mark.skipif(not os.environ.get('CHALLENGEBOT_LOAD_MEMBERS'), reason='Opt-in synthetic load check')
def test_scoring_scale_round_trip(tmp_path):
    members = int(os.environ['CHALLENGEBOT_LOAD_MEMBERS'])
    assert 1 <= members <= 1000, 'Use 1..1000 synthetic members'
    database = str(tmp_path / 'synthetic-load.sqlite')
    timings = {}

    def timed(name, call):
        start = perf_counter()
        result = call()
        timings[name] = round(perf_counter() - start, 3)
        return result

    db.init_db(database)
    originals = {}
    expected_changes, expected_opens = set(), set()
    with db.db_session() as conn:
        # Four independent editions: inherited and absolute Monthly/Weekly.
        for number, (duration, override) in enumerate([
            ('monthly', None), ('monthly', 17), ('weekly', None), ('weekly', 19),
        ], start=1):
            cid = f'load-{number}'
            conn.execute('''INSERT INTO challenge_defs
                (id,year,tier,number,title,goal_kind,goal_amount,duration_group,points,edition_id,identity_id)
                VALUES(?,2026,'easy',?,?,'minutes',1,?,?,?,?)''',
                         (cid, number, cid, duration, override, cid, cid))
        # Exercise real catalog backfill and receipt synchronization, not mocks.
        scoring._catalog_records(conn)
        definitions = [dict(row) for row in conn.execute('SELECT * FROM challenge_defs ORDER BY id')]
        for member in range(members):
            for ch in definitions:
                rules = json.dumps(ch)
                snapshot = scoring._json(scoring.capture_snapshot(conn, ch))
                for status in ('completed', 'active', 'scheduled'):
                    completed = '2026-01-20T00:00:00+00:00' if status == 'completed' else None
                    aid = conn.execute('''INSERT INTO attempts
                        (discord_id,challenge_id,status,started_at,ends_at,completed_at,actual_completed_at,
                         rules_snapshot_json,scoring_snapshot_json)
                        VALUES(?,?,?,'2026-01-01T00:00:00+00:00','2026-02-01T00:00:00+00:00',?,?,?,?)''',
                                       (f'load-member-{member}', ch['id'], status, completed, completed,
                                        rules, snapshot)).lastrowid
                    originals[aid] = snapshot
                    if status == 'completed':
                        scoring.ensure_completion_award(conn, aid)
                    if ch['duration_group'] == 'monthly' and ch['points'] is None:
                        if status == 'completed':
                            expected_changes.add(f'completion:{aid}')
                        else:
                            expected_opens.add(aid)

    def state():
        with db.db_session() as conn:
            return {
                'awards': [tuple(row) for row in conn.execute(
                    'SELECT source_key,original_points,effective_points,current_policy_id FROM award_ledger ORDER BY source_key')],
                'snapshots': {row['id']: row['scoring_snapshot_json'] for row in conn.execute('SELECT * FROM attempts')},
                'rules': [tuple(row) for row in conn.execute('SELECT id,rules_snapshot_json FROM attempts ORDER BY id')],
                'totals': [tuple(row) for row in conn.execute(
                    'SELECT discord_id,SUM(effective_points) FROM award_ledger GROUP BY discord_id ORDER BY discord_id')],
            }

    def make_preview():
        return scoring.preview_recalculation({'multipliers': {'monthly': 5}},
                                             {'edition_ids': ['load-1', 'load-2']}, include_open=True)

    # Preview may materialize baseline receipts; settle those before comparison.
    timed('warmup_preview', make_preview)
    baseline = state()
    with db.db_session() as conn:
        baseline_operations = conn.execute('SELECT COUNT(*) FROM admin_operations').fetchone()[0]
    preview = timed('preview', make_preview)
    assert {c['source_key'] for c in preview['changes']} == expected_changes
    assert {a['attempt_id'] for a in preview['open_attempts']} == expected_opens
    assert state() == baseline
    for total in preview['totals'].values():
        assert total['delta'] == 2  # Easy Monthly compressed default 3 -> 5.

    def apply():
        return scoring.apply_recalculation(preview, actor='synthetic-load', reason='Disposable scale validation',
                                           operation_id='load-apply')

    result = timed('apply', apply)
    assert result['adjusted'] == members
    assert len(result['open_snapshots']) == members * 2
    applied = state()
    assert applied['rules'] == baseline['rules']
    baseline_awards = {row[0]: row for row in baseline['awards']}
    for source, original, effective, _ in applied['awards']:
        before = baseline_awards[source]
        assert original == before[1]
        assert effective == before[2] + (2 if source in expected_changes else 0)
    for aid, snapshot in applied['snapshots'].items():
        if aid in expected_opens:
            assert json.loads(snapshot)['points'] == 5
        else:
            assert snapshot == originals[aid]
    assert timed('apply_retry', apply) == result
    assert state() == applied
    timed('restart_after_apply', lambda: db.init_db(database))
    assert state() == applied

    def revert():
        return scoring.revert_operation('load-apply', actor='synthetic-load', reason='Disposable scale validation',
                                        revert_operation_id='load-revert')

    reverted = timed('revert', revert)
    assert state() == baseline
    assert timed('revert_retry', revert) == reverted
    timed('restart_after_revert', lambda: db.init_db(database))
    assert state() == baseline
    with db.db_session() as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('SELECT COUNT(*) FROM point_adjustments').fetchone()[0] == members * 2
        assert conn.execute('SELECT COUNT(*) FROM admin_operations').fetchone()[0] == baseline_operations + 2
    print(json.dumps({'members': members, 'attempts': members * 12, 'seconds': timings}, sort_keys=True))
