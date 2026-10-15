from __future__ import annotations

import sqlite3
import threading
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
    ("tadoku_approved_at", "ALTER TABLE users ADD COLUMN tadoku_approved_at TEXT"),
    ("tadoku_approved_by", "ALTER TABLE users ADD COLUMN tadoku_approved_by TEXT"),
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
        _migrate(conn)
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
