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
-- app/db.py adds a unique partial index on non-null supersedes_roster_id
-- after adding the column to existing databases. Repair chains cannot branch.
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
