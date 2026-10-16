"""Pre-lifecycle schema fixtures and private reconciliation, no runtime data."""
import importlib.util
import json
import sqlite3
from pathlib import Path

from app import db
from app.services import attempts

spec = importlib.util.spec_from_file_location('reconciliation', Path(__file__).parents[1] / 'scripts/reconcile_lifecycle.py')
reconciliation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconciliation)


def test_old_years_repeats_and_unknown_dates_reconcile(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    for cid, year, tier, number in [('old', 2025, 'easy', 1), ('current', 2026, 'ota_master', 1)]:
        conn.execute('INSERT INTO challenge_defs(id,year,tier,number,title,goal_kind,goal_amount) VALUES(?,?,?,?,?,?,?)',
                     (cid, year, tier, number, cid, 'pages', 10))
    for cid in ['old', 'old', 'current']:
        conn.execute('INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at,completed_at,verify_method) VALUES(?,?,?,?,?,?,?)',
                     ('synthetic', cid, 'completed', '2025-01-01T00:00:00+00:00', '2025-01-02T00:00:00+00:00', '2025-01-01T12:00:00+00:00', 'honor'))
    conn.commit()
    conn.close()
    before = reconciliation.report(path)
    assert before['effective_points'] == 14  # 1+1+4, latest-year bonus8 only
    db.init_db(str(path))
    after = reconciliation.report(path)
    assert reconciliation.compare(before, after)['clean']
    assert attempts.challenge_points('synthetic') == 14
    with db.db_session() as conn:
        row = dict(conn.execute('SELECT * FROM attempts WHERE id=1').fetchone())
        assert row['actual_completed_at'] is None
        assert row['confirmed_at'] == row['completed_at']
        assert row['original_deadline'] == row['ends_at']
        assert row['provenance'] == 'legacy_baseline'
        assert json.loads(row['rules_snapshot_json'])['title'] == 'old'
        conn.execute("UPDATE challenge_defs SET title='Future title',verify='tadoku',tier='hard' WHERE id='old'")
    assert attempts.get_attempt(1)['title'] == 'old'
    db.init_db(str(path))
    assert attempts.challenge_points('synthetic') == 14
    assert reconciliation.compare(after, reconciliation.report(path))['clean']


def test_incompatible_ladder_is_flagged_without_shortening_saved_deadline(tmp_path):
    path = tmp_path / 'legacy-long.sqlite'
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO challenge_defs(id,year,tier,number,title,time_limit_hours,goal_kind,goal_amount) VALUES('long',2026,'easy',1,'Legacy long',48,'pages',10)")
    conn.execute("INSERT INTO attempts(discord_id,challenge_id,status,started_at,ends_at) VALUES('synthetic','long','active','2026-01-01T00:00:00+00:00','2026-01-03T00:00:00+00:00')")
    conn.commit()
    conn.close()
    db.init_db(str(path))
    with db.db_session() as conn:
        definition = conn.execute("SELECT * FROM challenge_defs WHERE id='long'").fetchone()
        saved = conn.execute('SELECT * FROM attempts WHERE id=1').fetchone()
        assert definition['duration_group'] == 'special'
        assert saved['ends_at'] == '2026-01-03T00:00:00+00:00'
        assert saved['original_deadline'] == saved['ends_at']
    assert reconciliation.report(path)['migration_review_required']
