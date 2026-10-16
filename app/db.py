from __future__ import annotations

import sqlite3
import threading
import json
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

_lock = threading.RLock()
_db_path: str = "./data/challenges.db"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

# SQLite binds overflow above 2^63-1; counters stay in signed 32-bit.
SQL_INT_MAX = 2**63 - 1
COUNTER_MAX = 2**31 - 1


def sql_int(
    n: Any,
    *,
    lo: int = 0,
    hi: int = COUNTER_MAX,
    default: int | None = None,
) -> int:
    """Clamp an int for SQL binds / counters so it never hits max int."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return lo if default is None else default
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def configure(path: str) -> None:
    global _db_path
    _db_path = path
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def database_path() -> Path:
    return Path(_db_path)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  discord_id TEXT PRIMARY KEY,
  display_name TEXT,
  tadoku_user_id TEXT,
  tadoku_name TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS challenge_defs (
  id TEXT PRIMARY KEY,
  year INTEGER NOT NULL,
  tier TEXT NOT NULL,
  number INTEGER NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  time_limit_hours REAL NOT NULL DEFAULT 24,
  param_label TEXT,
  verify TEXT NOT NULL DEFAULT 'tadoku',
  goal_kind TEXT NOT NULL,
  goal_amount REAL NOT NULL,
  media_filter TEXT NOT NULL DEFAULT 'any',
  allows_concurrent INTEGER NOT NULL DEFAULT 0,
  fixed_param TEXT,
  points INTEGER,
  category TEXT NOT NULL DEFAULT 'ota' CHECK(category IN ('ota', 'physical')),
  UNIQUE(year, tier, number)
);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  discord_id TEXT NOT NULL,
  challenge_id TEXT NOT NULL,
  param TEXT,
  content TEXT,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ends_at TEXT NOT NULL,
  completed_at TEXT,
  verify_method TEXT,
  evidence_json TEXT,
  guild_id TEXT,
  note TEXT,
  FOREIGN KEY (challenge_id) REFERENCES challenge_defs(id)
);

CREATE INDEX IF NOT EXISTS idx_attempts_user_status ON attempts(discord_id, status);
CREATE INDEX IF NOT EXISTS idx_attempts_status_ends ON attempts(status, ends_at);
CREATE INDEX IF NOT EXISTS idx_attempts_challenge ON attempts(challenge_id);

CREATE TABLE IF NOT EXISTS congrats_usage (
  path TEXT PRIMARY KEY,
  use_count INTEGER NOT NULL DEFAULT 0,
  last_used TEXT
);

CREATE TABLE IF NOT EXISTS challenge_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  challenge_id TEXT NOT NULL,
  change TEXT NOT NULL CHECK(change IN ('new', 'updated', 'retired')),
  before_json TEXT,
  after_json TEXT,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  announced_at TEXT
);

CREATE TABLE IF NOT EXISTS achievement_defs (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  rule_json TEXT NOT NULL,
  image_file TEXT,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
  position INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_achievements (
  discord_id TEXT NOT NULL,
  achievement_id TEXT NOT NULL REFERENCES achievement_defs(id),
  unlocked_at TEXT,
  source_attempt_id INTEGER REFERENCES attempts(id),
  PRIMARY KEY(discord_id, achievement_id)
);
"""

LIFECYCLE_SCHEMA = """
-- Shared backend schema contract. Executed additively by app/db.py.
-- IDs are TEXT except attempt IDs/revision/event sequence IDs. JSON is TEXT.
-- Nullable timestamps represent unknown facts, never fabricated completion dates.
CREATE TABLE IF NOT EXISTS challenge_identities (
 id TEXT PRIMARY KEY, legacy_id TEXT UNIQUE, lifecycle_state TEXT NOT NULL DEFAULT 'active',
 repeat_policy TEXT NOT NULL DEFAULT 'legacy', created_at TEXT, recorded_at TEXT,
 retired_at TEXT, retirement_reason TEXT, available_from TEXT, available_until TEXT, restore_at TEXT,
 actor TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS challenge_editions (
 id TEXT PRIMARY KEY, identity_id TEXT NOT NULL REFERENCES challenge_identities(id),
 challenge_id TEXT UNIQUE REFERENCES challenge_defs(id), year INTEGER NOT NULL,
 catalog_year INTEGER, display_number INTEGER, lifecycle_state TEXT NOT NULL DEFAULT 'active',
 published_at TEXT, available_from TEXT, available_until TEXT, retired_at TEXT, restore_at TEXT,
 revision_id INTEGER, created_at TEXT, updated_at TEXT, UNIQUE(identity_id, year)
);
CREATE TABLE IF NOT EXISTS definition_revisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, challenge_id TEXT, identity_id TEXT, edition_id TEXT,
 predecessor_id INTEGER, before_json TEXT, after_json TEXT, snapshot_json TEXT,
 recorded_at TEXT, effective_at TEXT, actor TEXT, source TEXT, reason TEXT, operation_id TEXT
);
CREATE TABLE IF NOT EXISTS lifecycle_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, identity_id TEXT, edition_id TEXT, challenge_id TEXT,
 event_type TEXT, before_json TEXT, after_json TEXT, effective_at TEXT, recorded_at TEXT,
 actor TEXT, source TEXT, reason TEXT, operation_id TEXT
);
CREATE TABLE IF NOT EXISTS attempt_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id INTEGER REFERENCES attempts(id),
 event_type TEXT, before_json TEXT, after_json TEXT, effective_at TEXT, recorded_at TEXT,
 actor TEXT, source TEXT, reason TEXT, operation_id TEXT
);
CREATE TABLE IF NOT EXISTS scoring_policies (
 id INTEGER PRIMARY KEY AUTOINCREMENT, policy_json TEXT NOT NULL, scope_json TEXT NOT NULL DEFAULT '{}',
 effective_at TEXT, recorded_at TEXT NOT NULL, actor TEXT, reason TEXT, operation_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS award_ledger (
 source_key TEXT PRIMARY KEY, discord_id TEXT NOT NULL, attempt_id INTEGER REFERENCES attempts(id),
 badge_receipt_id INTEGER, identity_id TEXT, edition_id TEXT, edition_year INTEGER,
 award_type TEXT NOT NULL DEFAULT 'completion', original_points INTEGER NOT NULL,
 effective_points INTEGER NOT NULL, original_policy_id INTEGER, current_policy_id INTEGER,
 original_awarded_at TEXT, recorded_at TEXT NOT NULL, provenance TEXT,
 metadata_json TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_award_user ON award_ledger(discord_id);
CREATE TABLE IF NOT EXISTS point_adjustments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL REFERENCES award_ledger(source_key),
 old_points INTEGER NOT NULL, new_points INTEGER NOT NULL, delta INTEGER NOT NULL,
 operation_id TEXT NOT NULL, actor TEXT, reason TEXT, effective_at TEXT, recorded_at TEXT NOT NULL,
 UNIQUE(operation_id, source_key)
);
CREATE TABLE IF NOT EXISTS admin_operations (
 operation_id TEXT PRIMARY KEY, operation_type TEXT NOT NULL, payload_json TEXT NOT NULL,
 result_json TEXT, actor TEXT, reason TEXT, effective_at TEXT, recorded_at TEXT NOT NULL,
 reverts_operation_id TEXT
);
CREATE TABLE IF NOT EXISTS daily_rosters (
 id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, tier TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, requirements_json TEXT NOT NULL DEFAULT '[]',
 published_at TEXT, recorded_at TEXT, actor TEXT, reason TEXT, rule_version TEXT DEFAULT '1',
 supersedes_roster_id INTEGER REFERENCES daily_rosters(id),
 UNIQUE(year, tier, version)
);
CREATE TABLE IF NOT EXISTS badge_receipts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, discord_id TEXT NOT NULL, duration_group TEXT NOT NULL,
 tier TEXT NOT NULL, scope TEXT NOT NULL, year INTEGER, roster_id INTEGER REFERENCES daily_rosters(id),
 qualifying_attempts_json TEXT NOT NULL DEFAULT '[]', requirements_json TEXT,
 unlocked_at TEXT, issued_at TEXT, recorded_at TEXT, rule_version TEXT DEFAULT '1',
 status TEXT NOT NULL DEFAULT 'active', revoked_at TEXT, provenance TEXT,
 UNIQUE(discord_id, duration_group, tier, scope)
);
CREATE TABLE IF NOT EXISTS badge_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id INTEGER REFERENCES badge_receipts(id),
 event_type TEXT, before_json TEXT, after_json TEXT, reason TEXT, actor TEXT,
 operation_id TEXT, effective_at TEXT, recorded_at TEXT
);
"""

# Additive columns for DBs created before content / tz existed.
_ATTEMPT_MIGRATIONS = (
    ("content", "ALTER TABLE attempts ADD COLUMN content TEXT"),
    # Milestone flags for automatic channel status posts
    ("status_json", "ALTER TABLE attempts ADD COLUMN status_json TEXT"),
    # Manual Tadoku fixup: {"aliases":[...],"log_ids":[...]}
    ("fixup_json", "ALTER TABLE attempts ADD COLUMN fixup_json TEXT"),
    ("challenge_snapshot_json", "ALTER TABLE attempts ADD COLUMN challenge_snapshot_json TEXT"),
)
_USER_MIGRATIONS = (
    ("tz_offset", "ALTER TABLE users ADD COLUMN tz_offset TEXT"),
)
_CHALLENGE_MIGRATIONS = (
    (
        "category",
        "ALTER TABLE challenge_defs ADD COLUMN category TEXT NOT NULL DEFAULT 'ota' "
        "CHECK(category IN ('ota', 'physical'))",
    ),
    (
        "allows_concurrent",
        "ALTER TABLE challenge_defs ADD COLUMN allows_concurrent "
        "INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "fixed_param",
        "ALTER TABLE challenge_defs ADD COLUMN fixed_param TEXT",
    ),
    (
        "points",
        "ALTER TABLE challenge_defs ADD COLUMN points INTEGER",
    ),
)


_LIFECYCLE_ATTEMPT_COLUMNS = {
    **{k: 'TEXT' for k in (
        'identity_id', 'edition_id', 'rules_snapshot_json', 'scoring_snapshot_json',
        'original_deadline', 'original_started_at', 'scheduled_start_original',
        'created_at', 'confirmed_at', 'verified_at', 'actual_completed_at',
        'original_completion_json', 'effective_completion_json', 'provenance',
        'calendar_timezone', 'calculation_version',
    )},
    'edition_year': 'INTEGER', 'definition_revision_id': 'INTEGER',
    'correction_revision': 'INTEGER NOT NULL DEFAULT 0',
}
_LIFECYCLE_CHALLENGE_COLUMNS = {
    **{k: 'TEXT' for k in ('identity_id', 'edition_id', 'available_from',
                          'available_until', 'retired_at', 'published_at')},
    'duration_group': "TEXT NOT NULL DEFAULT 'daily'",
    'repeat_policy': "TEXT NOT NULL DEFAULT 'legacy'",
    'lifecycle_state': "TEXT NOT NULL DEFAULT 'active'",
    'revision_id': 'INTEGER',
}


def _add_columns(conn, table, columns):
    existing = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')


def _migrate_lifecycle(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('catalog_calendar_timezone','UTC')")
    _add_columns(conn, 'attempts', _LIFECYCLE_ATTEMPT_COLUMNS)
    _add_columns(conn, 'challenge_defs', _LIFECYCLE_CHALLENGE_COLUMNS)
    # execute(), not executescript(): keep the entire migration in one transaction.
    for statement in LIFECYCLE_SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)
    for table in ('challenge_defs', 'challenge_identities', 'challenge_editions'):
        _add_columns(conn, table, {'restore_at': 'TEXT'})
    _add_columns(conn, 'daily_rosters', {
        'supersedes_roster_id': 'INTEGER REFERENCES daily_rosters(id)',
    })
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_roster_repair_parent ON daily_rosters(supersedes_roster_id) WHERE supersedes_roster_id IS NOT NULL')
    first_migration = not conn.execute("SELECT 1 FROM meta WHERE key='lifecycle_schema_v1'").fetchone()
    if first_migration:
        conn.execute("UPDATE challenge_defs SET duration_group='special' WHERE LOWER(tier)='special'")
        review = []
        for definition in conn.execute("SELECT id,time_limit_hours FROM challenge_defs WHERE duration_group='daily'").fetchall():
            hours = definition['time_limit_hours']
            if hours is None or not math.isfinite(float(hours)) or not 0 < float(hours) <= 24:
                review.append({'challenge_id': definition['id'], 'time_limit_hours': hours,
                               'reason': 'Legacy ladder window is incompatible with Daily; kept as custom Special duration pending review'})
                conn.execute("UPDATE challenge_defs SET duration_group='special' WHERE id=?", (definition['id'],))
        if review:
            conn.execute("INSERT INTO meta(key,value) VALUES('lifecycle_review_required',?)", (json.dumps(review),))
        conn.execute("UPDATE attempts SET original_started_at=started_at, original_deadline=ends_at, "
                     "confirmed_at=completed_at, provenance='legacy_baseline', "
                     "calendar_timezone='UTC', calculation_version='legacy'")
        # completed_at in the old system is confirmation time, not a known actual clear.
        conn.execute("INSERT INTO meta(key,value) VALUES('lifecycle_schema_v1',?)", (utcnow_iso(),))
    from app.services import scoring
    from app.services.lifecycle import materialize_restorations
    materialize_restorations(conn)
    scoring._catalog_records(conn)
    if first_migration:
        # Capture a labeled reproducible baseline BEFORE any startup catalog
        # import. This is not evidence of originally advertised historical rules.
        definitions = {r['id']: dict(r) for r in conn.execute('SELECT * FROM challenge_defs')}
        for row in conn.execute('SELECT * FROM attempts').fetchall():
            attempt = dict(row)
            challenge = definitions.get(attempt['challenge_id'])
            if not challenge:
                continue  # Read-only reconciliation reports orphaned references.
            rules = dict(challenge)
            if attempt.get('challenge_snapshot_json'):
                snapshot = json.loads(attempt['challenge_snapshot_json'])
                rules.update({k: v for k, v in snapshot.items() if v is not None and k != 'id'})
            score = scoring.capture_snapshot(conn, challenge)
            conn.execute('UPDATE attempts SET identity_id=?,edition_id=?,edition_year=?,definition_revision_id=?,rules_snapshot_json=COALESCE(rules_snapshot_json,?),scoring_snapshot_json=COALESCE(scoring_snapshot_json,?) WHERE id=?',
                         (challenge['identity_id'], challenge['edition_id'], rules.get('year'), challenge['revision_id'],
                          json.dumps(rules), json.dumps(score), attempt['id']))
    scoring.bootstrap(conn)
    scoring.upgrade_duration_defaults(conn)
    from app.services import badges
    badges.publish_rosters(conn)
    for row in conn.execute('SELECT DISTINCT discord_id FROM attempts').fetchall():
        badges.sync_user(conn, row['discord_id'])


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {
        r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()
    }
    for name, ddl in _ATTEMPT_MIGRATIONS:
        if name not in cols:
            conn.execute(ddl)
    # Backfill content from declared focus when empty.
    conn.execute(
        "UPDATE attempts SET content = param "
        "WHERE (content IS NULL OR content = '') "
        "AND param IS NOT NULL AND param != ''"
    )
    ucols = {
        r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    for name, ddl in _USER_MIGRATIONS:
        if name not in ucols:
            conn.execute(ddl)
    ccols = {
        r[1] for r in conn.execute("PRAGMA table_info(challenge_defs)").fetchall()
    }
    for name, ddl in _CHALLENGE_MIGRATIONS:
        if name not in ccols:
            conn.execute(ddl)


def init_db(path: Optional[str] = None) -> None:
    if path:
        configure(path)
    with db_session() as conn:
        conn.executescript(SCHEMA)
        conn.execute('BEGIN IMMEDIATE')
        _migrate(conn)
        _migrate_lifecycle(conn)
        from app.services.achievements import bootstrap

        bootstrap(conn)


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def get_all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def set_settings(conn: sqlite3.Connection, mapping: dict[str, str]) -> None:
    for k, v in mapping.items():
        set_setting(conn, k, v if v is not None else "")


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return dict(row)
