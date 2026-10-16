"""Read-only report validates legacy and migrated isolated databases."""
import importlib.util
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location("reconcile_lifecycle", Path(__file__).parents[1] / "scripts/reconcile_lifecycle.py")
reconcile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconcile)


def fixture_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE challenge_defs(id TEXT PRIMARY KEY, year INTEGER, tier TEXT, points INTEGER);
        CREATE TABLE attempts(id INTEGER PRIMARY KEY, discord_id TEXT, challenge_id TEXT REFERENCES challenge_defs(id), status TEXT);
        INSERT INTO challenge_defs VALUES('one',2026,'easy',NULL);
        INSERT INTO attempts VALUES(1,'synthetic-account','one','completed');
        INSERT INTO attempts VALUES(2,'synthetic-account','one','completed');
    """)
    conn.commit()
    conn.close()


def test_legacy_baseline_preserves_repeats_and_sweep(tmp_path):
    path = tmp_path / "fixture.sqlite"
    fixture_db(path)
    before_bytes = path.read_bytes()
    result = reconcile.report(path)
    assert result["effective_points"] == 4  # two 1pt clears, one 2pt sweep
    assert result["completion_count"] == 2
    assert result["unknown_actual_completion_times"] == 2
    assert "synthetic-account" not in str(result)
    assert result["foreign_key_errors"] == []
    assert path.read_bytes() == before_bytes
    assert reconcile.compare(result, result)["clean"]


def test_ledger_becomes_effective_source_and_comparison_detects_drift(tmp_path):
    path = tmp_path / "fixture.sqlite"
    fixture_db(path)
    before = reconcile.report(path)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE award_ledger(discord_id TEXT, effective_points INTEGER);
        INSERT INTO award_ledger VALUES('synthetic-account',3);
    """)
    conn.close()
    after = reconcile.report(path)
    assert after["effective_points"] == 3
    assert after["legacy_query_points"] == 4
    diff = reconcile.compare(before, after)
    assert not diff["clean"]
    assert list(diff["member_point_deltas"].values()) == [-1]
