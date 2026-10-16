"""Audited challenge lifecycle and immutable attempt facts.

Connection-taking helpers never commit. Start validation and insertion must share
one BEGIN IMMEDIATE transaction. Administrative writes acquire that lock here.
Only UTC arithmetic is supported by calculation version ``utc-anniversary-v1``.
"""
from __future__ import annotations

import calendar
import hashlib
import importlib
import json
import math
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import db_session, utcnow_iso

CALCULATION_VERSION = "utc-anniversary-v1"
OPEN_STATUSES = ("active", "scheduled")
STATUSES = {*OPEN_STATUSES, "completed", "expired", "abandoned", "void", "cancelled"}
FACT_FIELDS = {
    "started_at", "ends_at", "actual_completed_at", "completed_at", "confirmed_at",
    "verified_at", "status", "param", "content", "evidence_json", "verify_method", "note",
}
CLASS_FIELDS = {"tier", "duration_group", "category", "media_filter"}
RULE_FIELDS = (
    "identity_id", "edition_id", "year", "edition_year", "revision_id", "tier", "number",
    "title", "description", "time_limit_hours", "param_label", "verify", "goal_kind",
    "goal_amount", "media_filter", "allows_concurrent", "fixed_param", "points", "category",
    "duration_group", "repeat_policy", "lifecycle_state", "published_at", "available_from",
    "available_until", "retired_at", "restore_at",
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _object(value: Any) -> dict:
    if isinstance(value, dict):
        return value.copy()
    return json.loads(value) if value else {}


def _date(value: str | datetime) -> datetime:
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid ISO timestamp") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return dt.astimezone(timezone.utc)


def _group(challenge: dict) -> str:
    return str(challenge.get("duration_group") or (
        "special" if str(challenge.get("tier", "")).lower() == "special" else "daily"
    )).lower()


def calculate_deadline(challenge: dict, start: datetime) -> datetime:
    """Calculate a UTC anniversary without rounding or changing the start instant."""
    challenge = dict(challenge)
    start = _date(start)
    group = _group(challenge)
    if group in {"monthly", "yearly"}:
        year, month = start.year, start.month
        if group == "monthly":
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        else:
            year += 1
        day = min(start.day, calendar.monthrange(year, month)[1])
        return start.replace(year=year, month=month, day=day)
    if group not in {"daily", "special"}:
        raise ValueError("Unknown duration group")
    try:
        hours = float(challenge.get("time_limit_hours", 24))
    except (TypeError, ValueError) as exc:
        raise ValueError("Duration must be positive hours") from exc
    if not math.isfinite(hours) or hours <= 0 or (group == "daily" and hours > 24):
        raise ValueError("Daily duration must be positive and at most 24 hours" if group == "daily"
                         else "Duration must be positive finite hours")
    return start + timedelta(hours=hours)


def ensure_catalog_records(conn) -> None:
    """Publish revision records for the current catalog, without resurrecting identities."""
    now = utcnow_iso()
    for row in conn.execute("SELECT * FROM challenge_defs ORDER BY id").fetchall():
        ch = dict(row)
        cid = ch["id"]
        identity = ch.get("identity_id") or cid
        edition = ch.get("edition_id") or cid
        state = ch.get("lifecycle_state") or "active"
        policy = ch.get("repeat_policy") or "legacy"
        conn.execute(
            "INSERT OR IGNORE INTO challenge_identities "
            "(id,legacy_id,lifecycle_state,repeat_policy,created_at,recorded_at,actor,source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (identity, cid, state, policy, now, now, "system", "catalog"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO challenge_editions "
            "(id,identity_id,challenge_id,year,catalog_year,display_number,lifecycle_state,"
            "published_at,available_from,available_until,retired_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (edition, identity, cid, ch["year"], ch["year"], ch["number"], state,
             ch.get("published_at"), ch.get("available_from"), ch.get("available_until"),
             ch.get("retired_at"), now, now),
        )
        existing = conn.execute("SELECT * FROM challenge_editions WHERE id=?", (edition,)).fetchone()
        if existing is None or existing["identity_id"] != identity or existing["challenge_id"] != cid:
            raise ValueError("Edition identity/year conflict; explicit edition IDs are required")
        # Identity/year uniqueness must never silently map a second definition to an old edition.
        ch.update(identity_id=identity, edition_id=edition, duration_group=_group(ch))
        snapshot = {key: ch.get(key) for key in RULE_FIELDS if key != "revision_id"}
        previous = conn.execute(
            "SELECT id,snapshot_json FROM definition_revisions WHERE edition_id=? ORDER BY id DESC LIMIT 1",
            (edition,),
        ).fetchone()
        encoded = _json(snapshot)
        revision = previous["id"] if previous else None
        if previous is None or previous["snapshot_json"] != encoded:
            cur = conn.execute(
                "INSERT INTO definition_revisions (challenge_id,identity_id,edition_id,predecessor_id,"
                "before_json,after_json,snapshot_json,recorded_at,effective_at,actor,source,reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, identity, edition, revision, previous["snapshot_json"] if previous else None,
                 encoded, encoded, now, now, "system", "catalog", "Catalog definition revision"),
            )
            revision = cur.lastrowid
        conn.execute(
            "UPDATE challenge_defs SET identity_id=?,edition_id=?,revision_id=?,duration_group=? WHERE id=?",
            (identity, edition, revision, _group(ch), cid),
        )
        conn.execute(
            "UPDATE challenge_editions SET revision_id=?,published_at=?,available_from=?,"
            "available_until=?,lifecycle_state=?,retired_at=?,updated_at=? WHERE id=?",
            (revision, ch.get("published_at"), ch.get("available_from"), ch.get("available_until"),
             state, ch.get("retired_at"), now, edition),
        )
        conn.execute("UPDATE challenge_identities SET repeat_policy=? WHERE id=?", (policy, identity))


def effective_rules(attempt) -> dict:
    """Overlay a joined verification row with immutable rules and explicit corrections."""
    result = dict(attempt)
    rules = _object(result.get("rules_snapshot_json"))
    # Never confuse a definition's ID with the attempt ID in a joined result.
    for key, value in rules.items():
        if key not in {"id", "challenge_id"}:
            result[key] = value
    effective = _object(result.get("effective_completion_json"))
    result.update({k: v for k, v in effective.items() if k in CLASS_FIELDS})
    return result


def _scoring():
    try:
        return importlib.import_module("app.services.scoring")
    except ModuleNotFoundError as exc:
        if exc.name != "app.services.scoring":
            raise
        return importlib.import_module("app.scoring")


def capture_attempt(conn, attempt_id: int, challenge: dict) -> None:
    """Capture once, immediately after insertion, inside the start transaction."""
    row = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown attempt")
    att = dict(row)
    if att.get("rules_snapshot_json"):
        return
    # The first implementation deliberately supports UTC only; never silently
    # reinterpret saved member UTC offsets or an edited catalog timezone.
    from app.db import get_setting
    if get_setting(conn, 'catalog_calendar_timezone', 'UTC') != 'UTC':
        raise ValueError('Only UTC catalog calendar policy is supported; saved deadlines remain unchanged')
    # Always use authoritative catalog metadata after catalog revision publication.
    ensure_catalog_records(conn)
    ch = dict(conn.execute("SELECT * FROM challenge_defs WHERE id=?", (att["challenge_id"],)).fetchone())
    rules = {k: ch.get(k) for k in RULE_FIELDS}
    rules.update(duration_group=_group(ch), edition_year=ch["year"], calendar_timezone="UTC",
                 calculation_version=CALCULATION_VERSION)
    scoring = _scoring()
    score = scoring.capture_snapshot(conn, ch, at=att["started_at"])
    conn.execute(
        "UPDATE attempts SET identity_id=?,edition_id=?,edition_year=?,definition_revision_id=?,"
        "rules_snapshot_json=?,scoring_snapshot_json=?,original_deadline=COALESCE(original_deadline,ends_at),"
        "original_started_at=COALESCE(original_started_at,started_at),"
        "scheduled_start_original=CASE WHEN status='scheduled' THEN started_at ELSE scheduled_start_original END,"
        "created_at=COALESCE(created_at,?),calendar_timezone='UTC',calculation_version=?,provenance=? WHERE id=?",
        (ch["identity_id"], ch["edition_id"], ch["year"], ch["revision_id"], _json(rules),
         score if isinstance(score, str) else _json(score), utcnow_iso(), CALCULATION_VERSION, "captured_start", attempt_id),
    )
    captured = dict(conn.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone())
    conn.execute('INSERT INTO attempt_events(attempt_id,event_type,after_json,effective_at,recorded_at,actor,source,reason) VALUES(?,?,?,?,?,?,?,?)',
                 (attempt_id, 'created', _json(captured), captured['created_at'], utcnow_iso(), captured['discord_id'], 'attempt_start', 'Accepted attempt rules, selected work and original timing'))


def _available(record: dict, start: datetime) -> None:
    retired = record.get("retired_at")
    restored = record.get('restore_at') and start >= _date(record['restore_at'])
    if restored:
        record = {**record, 'lifecycle_state': 'active', 'retired_at': None}
        retired = None
    # Scheduled retirement/restoration is represented by its effective instant.
    if record.get("lifecycle_state") == "retired" and (not retired or start >= _date(retired)):
        raise ValueError("Challenge is retired")
    if retired and start >= _date(retired):
        raise ValueError("Challenge is retired")
    for field in ("published_at", "available_from"):
        if record.get(field) and start < _date(record[field]):
            raise ValueError("Challenge is not yet available")
    if record.get("available_until") and start >= _date(record["available_until"]):
        raise ValueError("Challenge is no longer available")


def validate_start(conn, challenge: dict, discord_id: str, start: datetime) -> None:
    """Validate while caller holds BEGIN IMMEDIATE; does not implement legacy retry switch."""
    if not conn.in_transaction:
        raise ValueError("Start validation requires a BEGIN IMMEDIATE transaction")
    start = _date(start)
    cid = dict(challenge).get("id") or dict(challenge).get("challenge_id")
    raw = conn.execute("SELECT * FROM challenge_defs WHERE id=?", (cid,)).fetchone()
    if raw is None:
        raise ValueError("Unknown challenge")
    ch = dict(raw)
    identity = ch.get("identity_id") or cid
    edition_id = ch.get("edition_id") or cid
    identity_row = conn.execute("SELECT * FROM challenge_identities WHERE id=?", (identity,)).fetchone()
    edition_row = conn.execute("SELECT * FROM challenge_editions WHERE id=?", (edition_id,)).fetchone()
    if not identity_row or not edition_row:
        raise ValueError("Challenge edition has not been published")
    for record in (dict(identity_row), dict(edition_row), ch):
        _available(record, start)
    calculate_deadline(ch, start)  # Validate the definition even when merely scheduling.
    rows = conn.execute(
        "SELECT a.*,c.identity_id AS catalog_identity,c.duration_group AS catalog_duration,"
        "c.tier AS catalog_tier,c.allows_concurrent AS catalog_concurrent,c.year AS catalog_year "
        "FROM attempts a JOIN challenge_defs c ON c.id=a.challenge_id "
        "WHERE a.discord_id=? AND a.status IN ('active','scheduled','completed')", (str(discord_id),),
    ).fetchall()
    policy = ch.get("repeat_policy") or identity_row["repeat_policy"] or "legacy"
    for raw in rows:
        att = dict(raw)
        aid = att.get("identity_id") or att.get("catalog_identity") or att["challenge_id"]
        if aid == identity:
            if att["status"] in OPEN_STATUSES:
                raise ValueError("An attempt for this challenge identity is already open")
            if policy == "once_ever" or (policy == "annual" and (
                att.get("edition_year") or att["catalog_year"]) == ch["year"]):
                raise ValueError("Challenge completion eligibility has already been used")
        if att["status"] in OPEN_STATUSES and _group(ch) == "daily" and not ch.get("allows_concurrent"):
            rules = effective_rules(att)
            group = rules.get("duration_group") or att.get("catalog_duration") or (
                "special" if str(att["catalog_tier"]).lower() == "special" else "daily")
            concurrent = rules.get("allows_concurrent", att["catalog_concurrent"])
            if group == "daily" and not concurrent:
                raise ValueError("An exclusive Daily attempt is already open")


def _reason(actor, reason, operation_id):
    if not str(actor or "").strip() or not str(reason or "").strip() or not str(operation_id or "").strip():
        raise ValueError("Actor, reason and operation ID are required")


def _operation(conn, operation_id, kind, payload, actor, reason):
    row = conn.execute("SELECT * FROM admin_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row:
        if row["operation_type"] != kind or row["payload_json"] != _json(payload):
            raise ValueError("Operation ID was already used for a different request")
        return _object(row["result_json"])
    return None


def _record_operation(conn, operation_id, kind, payload, result, actor, reason):
    conn.execute(
        "INSERT INTO admin_operations (operation_id,operation_type,payload_json,result_json,actor,reason,"
        "effective_at,recorded_at) VALUES (?,?,?,?,?,?,?,?)",
        (operation_id, kind, _json(payload), _json(result), str(actor), reason, utcnow_iso(), utcnow_iso()),
    )


def _lifecycle(challenge_id, *, actor, reason, identity=False, effective_at=None,
               operation_id=None, restore=False, conn=None, record_operation=True):
    import uuid
    operation_id = operation_id or str(uuid.uuid4())
    _reason(actor, reason, operation_id)
    effective = _date(effective_at).isoformat() if effective_at else None
    payload = {"challenge_id": challenge_id, "identity": identity, "effective_at": effective,
               "restore": restore}
    owns_transaction = conn is None
    with (db_session() if owns_transaction else nullcontext(conn)) as conn:
        if owns_transaction:
            conn.execute('BEGIN IMMEDIATE')
        prior = _operation(conn, operation_id, 'lifecycle', payload, actor, reason) if record_operation else None
        if prior is not None:
            return prior
        materialize_restorations(conn)
        ensure_catalog_records(conn)
        row = conn.execute("SELECT * FROM challenge_defs WHERE id=?", (challenge_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown challenge")
        ch = dict(row)
        now = utcnow_iso()
        effective = effective or now
        table, key = ("challenge_identities", ch["identity_id"]) if identity else ("challenge_editions", ch["edition_id"])
        before = dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone())
        scheduled_restore = restore and _date(effective) > _date(now)
        if scheduled_restore and (before['lifecycle_state'] != 'retired' and not before.get('retired_at')):
            raise ValueError('Schedule a retirement before scheduling its restoration')
        if scheduled_restore and before.get('retired_at') and _date(effective) <= _date(before['retired_at']):
            raise ValueError('Restoration must be after retirement')
        if not restore and before.get('restore_at') and _date(before['restore_at']) > _date(now):
            raise ValueError('A restoration is already scheduled; cancel it explicitly before retiring again')
        state = before['lifecycle_state'] if scheduled_restore else ('active' if restore else 'retired')
        retired_at = before.get('retired_at') if scheduled_restore else (None if restore else effective)
        restore_at = effective if scheduled_restore else None
        conn.execute(f"UPDATE {table} SET lifecycle_state=?,retired_at=?,restore_at=? WHERE id=?",
                     (state, retired_at, restore_at, key))
        if identity:
            conn.execute("UPDATE challenge_identities SET retirement_reason=?,actor=?,source='admin' WHERE id=?",
                         (before.get('retirement_reason') if scheduled_restore else (None if restore else reason), str(actor), key))
        else:
            conn.execute("UPDATE challenge_defs SET lifecycle_state=?,retired_at=?,restore_at=? WHERE id=?",
                         (state, retired_at, restore_at, challenge_id))
        after = dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone())
        conn.execute(
            "INSERT INTO lifecycle_events (identity_id,edition_id,challenge_id,event_type,before_json,after_json,"
            "effective_at,recorded_at,actor,source,reason,operation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ch["identity_id"], None if identity else ch["edition_id"], challenge_id,
             "restored" if restore else "retired", _json(before), _json(after), effective, now,
             str(actor), "admin", reason, operation_id),
        )
        ensure_catalog_records(conn)
        from app.catalog_changes import record_change
        catalog_after = dict(conn.execute("SELECT * FROM challenge_defs WHERE id=?", (challenge_id,)).fetchone())
        if identity:
            # Permanent identity changes remain visible in the established catalog audit, too.
            audit_before, audit_after = dict(ch), dict(catalog_after)
            audit_before["identity_lifecycle_state"] = before["lifecycle_state"]
            audit_after["identity_lifecycle_state"] = after["lifecycle_state"]
            record_change(conn, audit_before, audit_after,
                          change="updated" if restore else "retired", note=reason)
        else:
            record_change(conn, ch, catalog_after, change="updated" if restore else "retired", note=reason)
        from app.services.badges import publish_rosters
        publish_rosters(conn)
        result = {"operation_id": operation_id, "before": before, "after": after}
        if record_operation:
            _record_operation(conn, operation_id, "lifecycle", payload, result, actor, reason)
        return result


def retire_challenge(challenge_id, *, actor, reason, identity=False, effective_at=None, operation_id=None):
    return _lifecycle(challenge_id, actor=actor, reason=reason, identity=identity,
                      effective_at=effective_at, operation_id=operation_id)


def restore_challenge(challenge_id, *, actor, reason, identity=False, effective_at=None, operation_id=None):
    return _lifecycle(challenge_id, actor=actor, reason=reason, identity=identity,
                      effective_at=effective_at, operation_id=operation_id, restore=True)


def materialize_restorations(conn, *, at=None) -> int:
    """Advance rebuildable lifecycle projections; recorded schedule events remain.

    No attempt/deadline or award is modified. Prospective rosters are published
    by the caller after this projection catches up, including after a restart.
    """
    now = _date(at).isoformat() if at else utcnow_iso()
    changed = 0
    for table in ('challenge_defs', 'challenge_identities', 'challenge_editions'):
        changed += conn.execute(f"UPDATE {table} SET lifecycle_state='active',retired_at=NULL,restore_at=NULL WHERE restore_at IS NOT NULL AND julianday(restore_at)<=julianday(?)", (now,)).rowcount
    return changed


def cancel_restoration(challenge_id, *, actor, reason, identity=False, operation_id=None):
    """Explicitly cancel a pending restoration without changing retirement."""
    import uuid
    operation_id = operation_id or str(uuid.uuid4())
    _reason(actor, reason, operation_id)
    payload = {'challenge_id': challenge_id, 'identity': bool(identity)}
    with db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        prior = _operation(conn, operation_id, 'cancel_restoration', payload, actor, reason)
        if prior is not None:
            return prior
        ch = conn.execute('SELECT * FROM challenge_defs WHERE id=?', (challenge_id,)).fetchone()
        if ch is None:
            raise ValueError('Unknown challenge')
        table, key = ('challenge_identities', ch['identity_id']) if identity else ('challenge_editions', ch['edition_id'])
        before = dict(conn.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone())
        if not before.get('restore_at') or _date(before['restore_at']) <= _date(utcnow_iso()):
            raise ValueError('No pending restoration to cancel')
        conn.execute(f'UPDATE {table} SET restore_at=NULL WHERE id=?', (key,))
        if not identity:
            conn.execute('UPDATE challenge_defs SET restore_at=NULL WHERE id=?', (challenge_id,))
        after = dict(conn.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone())
        conn.execute('INSERT INTO lifecycle_events(identity_id,edition_id,challenge_id,event_type,before_json,after_json,effective_at,recorded_at,actor,source,reason,operation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     (ch['identity_id'], None if identity else ch['edition_id'], challenge_id, 'restoration_cancelled', _json(before), _json(after), utcnow_iso(), utcnow_iso(), str(actor), 'admin', reason, operation_id))
        result = {'operation_id': operation_id, 'before': before, 'after': after}
        _record_operation(conn, operation_id, 'cancel_restoration', payload, result, actor, reason)
        return result


def cancel_restore_challenge(challenge_id, **kwargs):
    if kwargs.pop('effective_at', None) is not None:
        raise ValueError('Restoration cancellation is immediate')
    return cancel_restoration(challenge_id, **kwargs)


def _fingerprint(conn) -> str:
    """Conservative dependency token: reject edits anywhere in the dependency graph.

    Includes policies, receipts and achievements so preview/apply never races a
    simultaneous scoring/roster correction. Deliberately excludes operation logs.
    """
    tables = ("attempts", "challenge_defs", "challenge_identities", "challenge_editions",
              "definition_revisions", "scoring_policies", "award_ledger", "point_adjustments",
              "daily_rosters", "badge_receipts", "user_achievements", "achievement_defs")
    graph = {}
    for table in tables:
        graph[table] = sorted((_json(dict(r)) for r in conn.execute(f"SELECT * FROM {table}")))
    return hashlib.sha256(_json(graph).encode()).hexdigest()


def _corrected(att: dict, changes: dict, recompute_deadline: bool, exception_reason: str | None):
    unknown = changes.keys() - FACT_FIELDS - CLASS_FIELDS
    if unknown:
        raise ValueError(f"Unsupported correction fields: {', '.join(sorted(unknown))}")
    if "ends_at" in changes and recompute_deadline:
        raise ValueError("Choose explicit deadline or recompute, not both")
    after = att.copy()
    metadata = _object(att.get("effective_completion_json"))
    for key, value in changes.items():
        if key in CLASS_FIELDS:
            metadata[key] = value
        else:
            if key.endswith("_at") and value is not None:
                value = _date(value).isoformat()
            after[key] = value
    if "completed_at" in changes and "actual_completed_at" in changes:
        if after["completed_at"] != after["actual_completed_at"]:
            raise ValueError("Completion date aliases disagree")
    if "completed_at" in changes:
        after["actual_completed_at"] = after["completed_at"]
    elif "actual_completed_at" in changes:
        after["completed_at"] = after["actual_completed_at"]
    rules = effective_rules({**after, "effective_completion_json": _json(metadata)})
    if rules.get("duration_group") not in {"daily", "monthly", "yearly", "special"}:
        raise ValueError("Invalid duration group")
    if rules.get("category") not in {"ota", "physical"}:
        raise ValueError("Invalid category")
    if str(rules.get("tier", "")).lower().replace("-", "_") not in {
        "easy", "medium", "hard", "ota_master", "special"
    }:
        raise ValueError("Invalid tier")
    if "tier" in metadata:
        metadata["tier"] = str(metadata["tier"]).lower().replace("-", "_")
    if recompute_deadline:
        after["ends_at"] = calculate_deadline(rules, _date(after["started_at"])).isoformat()
    if after["status"] not in STATUSES:
        raise ValueError("Invalid attempt status")
    if not after.get("started_at") or not after.get("ends_at"):
        raise ValueError("Start and deadline are required")
    warnings = []
    start, end = _date(after["started_at"]), _date(after["ends_at"])
    if end <= start:
        warnings.append("Deadline must be after start")
    actual = after.get("actual_completed_at") or after.get("completed_at")
    if actual:
        completed = _date(actual)
        if not start <= completed < end:
            warnings.append("Completion is outside the half-open attempt window")
        for field in ("confirmed_at", "verified_at"):
            if after.get(field) and _date(after[field]) < completed:
                warnings.append(f"{field} precedes actual completion")
    if after["status"] == "completed" and not actual:
        warnings.append("Completed attempt has no known actual completion date")
    if after.get("evidence_json"):
        try:
            json.loads(after["evidence_json"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Evidence must be valid JSON") from exc
    if warnings and not str(exception_reason or "").strip():
        raise ValueError("; ".join(warnings) + "; explicit exception_reason required")
    metadata.update({k: after.get(k) for k in FACT_FIELDS})
    after["effective_completion_json"] = _json(metadata)
    return after, warnings


def _sync_dependents(conn, before, after, operation_id, actor, reason):
    scoring = _scoring()
    uid = before["discord_id"]
    old_rules, new_rules = effective_rules(before), effective_rules(after)
    classification_changed = any(old_rules.get(k) != new_rules.get(k) for k in ('tier', 'duration_group'))
    # Reinstatement and reclassification can both adjust one source. Keep the
    # intermediate restoration distinct, linked to the parent correction ID.
    state_operation = f'{operation_id}:reinstate' if classification_changed and before['status'] != 'completed' and after['status'] == 'completed' else operation_id
    scoring.sync_user(conn, uid, actor=actor, reason=reason, operation_id=state_operation)
    if after["status"] == "completed" and classification_changed:
        # Explicit semantic corrections reclassify effective awards, not original
        # accepted rules. Retain the accepted policy and absolute overrides.
        score = _object(before.get("scoring_snapshot_json"))
        receipt = conn.execute('SELECT * FROM award_ledger WHERE source_key=?', (f"completion:{before['id']}",)).fetchone()
        metadata = _object(receipt['metadata_json'])
        policy_id = receipt['current_policy_id']
        policy = (scoring.current_policy(conn, new_rules, through_id=policy_id)
                  if policy_id is not None else score.get("policy") or scoring.current_policy(conn))
        revised_rules = {**new_rules, 'points': metadata.get('points_override', score.get('points_override'))}
        points = scoring.points_for_challenge(revised_rules, policy)
        scoring.set_award(conn, f"completion:{before['id']}", points,
                          operation_id=operation_id, actor=actor, reason=reason)
        metadata.setdefault('original_metadata', dict(metadata))
        metadata.update({key: new_rules.get(key) for key in CLASS_FIELDS})
        conn.execute('UPDATE award_ledger SET metadata_json=? WHERE source_key=?',
                     (_json(metadata), f"completion:{before['id']}"))
    from app.services.badges import sync_user as sync_badges
    from app.services.achievements import sync_user as sync_achievements
    sync_badges(conn, uid, operation_id=operation_id, actor=actor, reason=reason)
    sync_achievements(conn, uid)


def _projection_state(conn, uid):
    return {
        "points": int(conn.execute(
            "SELECT COALESCE(SUM(effective_points),0) FROM award_ledger WHERE discord_id=?", (uid,)
        ).fetchone()[0]),
        "badges": [dict(r) for r in conn.execute(
            "SELECT duration_group,tier,scope,status,qualifying_attempts_json,unlocked_at "
            "FROM badge_receipts WHERE discord_id=? ORDER BY duration_group,tier,scope", (uid,))],
        "achievements": [dict(r) for r in conn.execute(
            "SELECT achievement_id,source_attempt_id,unlocked_at FROM user_achievements "
            "WHERE discord_id=? ORDER BY achievement_id", (uid,))],
    }


def _projection_preview(conn, before, after):
    """Run the actual dependency synchronizers inside a rolled-back savepoint."""
    conn.execute("SAVEPOINT lifecycle_preview")
    try:
        scoring = _scoring()
        scoring.sync_user(conn, before["discord_id"])
        original = _projection_state(conn, before["discord_id"])
        from app.services.achievements import completion_snapshot
        updates = {k: after.get(k) for k in FACT_FIELDS}
        updates.update(effective_completion_json=after["effective_completion_json"],
                       rules_snapshot_json=before["rules_snapshot_json"],
                       challenge_snapshot_json=completion_snapshot(effective_rules(after)),
                       correction_revision=int(before.get("correction_revision") or 0) + 1)
        conn.execute("UPDATE attempts SET " + ",".join(f"{k}=?" for k in updates) + " WHERE id=?",
                     (*updates.values(), before["id"]))
        _sync_dependents(conn, before, after, "preview:semantic-correction", "preview", "Preview correction")
        effective = _projection_state(conn, before["discord_id"])
        return {"before": original, "after": effective,
                "points_delta": effective["points"] - original["points"]}
    finally:
        conn.execute("ROLLBACK TO lifecycle_preview")
        conn.execute("RELEASE lifecycle_preview")


def _preview(conn, attempt_id, changes, recompute_deadline, exception_reason):
    row = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown attempt")
    before = dict(row)
    # Legacy attempts lack snapshots; use the definition only as explicitly marked baseline.
    if not before.get("rules_snapshot_json"):
        ch = conn.execute("SELECT * FROM challenge_defs WHERE id=?", (before["challenge_id"],)).fetchone()
        before["rules_snapshot_json"] = _json({**dict(ch), "duration_group": _group(dict(ch))})
    after, warnings = _corrected(before, changes, recompute_deadline, exception_reason)
    result = {"version": 1, "attempt_id": attempt_id, "changes": changes,
              "recompute_deadline": bool(recompute_deadline), "exception_reason": exception_reason,
              "fingerprint": _fingerprint(conn), "before": before, "after": after, "warnings": warnings,
              "dependencies": ["verification", "completion", "points", "badges", "achievements", "profiles"],
              "impact": _projection_preview(conn, before, after)}
    result["preview_token"] = hashlib.sha256(_json(result).encode()).hexdigest()
    return result


def preview_correction(attempt_id, changes, *, recompute_deadline=False, exception_reason=None):
    # Normalize datetime objects for stable preview/idempotency tokens.
    changes = {k: (_date(v).isoformat() if isinstance(v, datetime) else v) for k, v in changes.items()}
    with db_session() as conn:
        conn.execute("BEGIN")
        return _preview(conn, attempt_id, changes, recompute_deadline, exception_reason)


def apply_correction(preview, *, actor, reason, operation_id):
    """Revalidate and atomically write facts and all derived awards. Retry-safe."""
    _reason(actor, reason, operation_id)
    payload = {"preview_token": preview.get("preview_token"), "attempt_id": preview.get("attempt_id")}
    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = _operation(conn, operation_id, "correction", payload, actor, reason)
        if prior is not None:
            return prior
        current = _preview(conn, preview["attempt_id"], preview["changes"],
                           preview["recompute_deadline"], preview["exception_reason"])
        if _json(current) != _json(preview):
            raise ValueError("Correction preview is stale or modified; preview again")
        before, after = current["before"], current["after"]
        aid = current["attempt_id"]
        original = before.get("original_completion_json") or _json({
            **{k: before.get(k) for k in FACT_FIELDS},
            **{k: effective_rules(before).get(k) for k in CLASS_FIELDS},
        })
        # Achievement snapshots are corrected effective projections, never the immutable start rules.
        from app.services.achievements import completion_snapshot
        snapshot = completion_snapshot(effective_rules(after))
        updates = {k: after.get(k) for k in FACT_FIELDS}
        updates.update(effective_completion_json=after["effective_completion_json"],
                       original_completion_json=original, challenge_snapshot_json=snapshot,
                       correction_revision=int(before.get("correction_revision") or 0) + 1,
                       rules_snapshot_json=before["rules_snapshot_json"],
                       original_deadline=before.get("original_deadline") or before["ends_at"],
                       original_started_at=before.get("original_started_at") or before["started_at"])
        if before["started_at"] != after["started_at"] or before["ends_at"] != after["ends_at"]:
            # Retain delivered milestone/DM markers across deadline revisions.
            # Only evidence caches and polling freshness are invalidated.
            status_meta = _object(before.get("status_json"))
            status_meta.update(goal_ok=False, last_progress=None, last_tadoku_poll_at=None,
                               deadline_revision=updates["correction_revision"])
            updates["status_json"] = _json(status_meta)
        conn.execute("UPDATE attempts SET " + ",".join(f"{k}=?" for k in updates) + " WHERE id=?",
                     (*updates.values(), aid))
        now = utcnow_iso()
        conn.execute(
            "INSERT INTO attempt_events (attempt_id,event_type,before_json,after_json,effective_at,recorded_at,"
            "actor,source,reason,operation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, "correction", _json(before), _json(after), now, now, str(actor), "admin",
             reason + (f"; date exception: {current['exception_reason']}" if current["exception_reason"] else ""), operation_id),
        )
        _sync_dependents(conn, before, after, operation_id, str(actor), reason)
        result = {"operation_id": operation_id, "attempt_id": aid,
                  "correction_revision": updates["correction_revision"], "warnings": current["warnings"]}
        _record_operation(conn, operation_id, "correction", payload, result, actor, reason)
        return result
