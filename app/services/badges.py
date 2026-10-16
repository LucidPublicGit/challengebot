"""Completion-based duration badges and durable, versioned Daily sweep receipts.

All writes are transaction-local; callers commit completions, corrections, ledger
adjustments and receipts together. Ordinary catalog edits never reclassify clears.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.db import db_session, utcnow_iso
from app.catalog import sweep_bonus

TIERS = ("easy", "medium", "hard", "ota_master")
GLYPHS = {"daily": "日", "monthly": "月", "yearly": "年"}
RULE_VERSION = "duration-badges-v1"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _object(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def publish_rosters(conn: sqlite3.Connection, *, at=None) -> list[int]:
    """Publish changed required sets, including Physical; preserve all old versions.

    Call at catalog publication/retirement boundaries, not on read. Empty versions
    intentionally supersede old requirements but can never qualify for an award.
    """
    now = at.astimezone(timezone.utc) if isinstance(at, datetime) else (_date(at) if at else datetime.now(timezone.utc))
    if now is None:
        raise ValueError('Invalid roster publication instant')
    groups = {}
    identities = {r['id']: dict(r) for r in conn.execute('SELECT * FROM challenge_identities')}
    for row in conn.execute("SELECT * FROM challenge_defs"):
        c = dict(row)
        tier = str(c.get("tier", "")).lower().replace("-", "_")
        duration = c.get("duration_group") or ("special" if tier == "special" else "daily")
        if tier not in TIERS or duration != "daily":
            continue
        key = (int(c["year"]), tier)
        requirements = groups.setdefault(key, [])
        identity = identities.get(c.get('identity_id'), {})
        identity_restored = _date(identity.get('restore_at'))
        if identity_restored and identity_restored <= now:
            identity = {**identity, 'lifecycle_state': 'active', 'retired_at': None}
        identity_retired = _date(identity.get('retired_at'))
        if identity_retired and identity_retired <= now:
            continue
        if identity.get('lifecycle_state') == 'retired' and identity_retired is None:
            continue
        restored_at = _date(c.get('restore_at'))
        if restored_at and restored_at <= now:
            c = {**c, 'lifecycle_state': 'active', 'retired_at': None}
        retired_at = _date(c.get("retired_at"))
        if c.get("lifecycle_state", "active") == "retired" and (retired_at is None or retired_at <= now):
            continue
        if retired_at and retired_at <= now:
            continue
        if _date(c.get("published_at")) and _date(c["published_at"]) > now:
            continue
        if _date(c.get("available_from")) and _date(c["available_from"]) > now:
            continue
        if _date(c.get("available_until")) and _date(c["available_until"]) <= now:
            continue
        requirements.append(str(c.get("edition_id") or c["id"]))
    # Also publish empty versions when the final requirement changes classification.
    for r in conn.execute("SELECT DISTINCT year,tier FROM daily_rosters"):
        groups.setdefault((r["year"], r["tier"]), [])
    published = []
    for (year, tier), requirements in sorted(groups.items()):
        requirements = sorted(set(requirements))
        previous = conn.execute(
            "SELECT * FROM daily_rosters WHERE year=? AND tier=? AND supersedes_roster_id IS NULL ORDER BY version DESC LIMIT 1",
            (year, tier),
        ).fetchone()
        if previous and json.loads(previous["requirements_json"]) == requirements:
            continue
        cur = conn.execute(
            "INSERT INTO daily_rosters(year,tier,version,requirements_json,published_at,rule_version) "
            "VALUES (?,?,?,?,?,?)",
            (year, tier, conn.execute('SELECT COALESCE(MAX(version),0)+1 FROM daily_rosters WHERE year=? AND tier=?', (year, tier)).fetchone()[0],
             _json(requirements), utcnow_iso(), RULE_VERSION),
        )
        published.append(cur.lastrowid)
    return published


def _completions(conn, discord_id):
    definitions = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM challenge_defs")}
    result = []
    for row in conn.execute("SELECT * FROM attempts WHERE discord_id=? AND status='completed'", (discord_id,)):
        a = dict(row)
        # Mutable definitions are a legacy fallback only. Snapshots and deliberate
        # semantic corrections take precedence (including explicit NULL dates).
        facts = dict(definitions.get(a["challenge_id"], {}))
        legacy_snapshot = _object(a.get("challenge_snapshot_json"))
        if not legacy_snapshot and not _object(a.get("rules_snapshot_json")) and facts:
            # Direct legacy inserts need a durable, explicitly inferred baseline
            # too; otherwise their next ordinary catalog edit could revoke them.
            legacy_snapshot = dict(facts)
            conn.execute(
                "UPDATE attempts SET challenge_snapshot_json=?,provenance=COALESCE(provenance,'legacy_badge_baseline') WHERE id=?",
                (_json(legacy_snapshot), a["id"]),
            )
        if legacy_snapshot and "duration_group" not in legacy_snapshot:
            # Pre-duration snapshots describe Daily ladders or Special entries,
            # not whichever duration the editable definition has become today.
            legacy_snapshot["duration_group"] = ("special" if str(legacy_snapshot.get("tier", "")).lower() == "special" else "daily")
        facts.update(legacy_snapshot)
        facts.update(_object(a.get("rules_snapshot_json")))
        facts.update(_object(a.get("effective_completion_json")))
        tier = str(facts.get("tier", "")).lower().replace("-", "_")
        duration = facts.get("duration_group") or ("special" if tier == "special" else "daily")
        if tier not in TIERS or duration not in GLYPHS:
            continue
        effective = _object(a.get("effective_completion_json"))
        if "actual_completed_at" in effective:
            completed = effective["actual_completed_at"]
        elif "completed_at" in effective:
            completed = effective["completed_at"]
        else:
            # Once actual_completed_at exists, NULL means genuinely unknown;
            # completed_at may be only a legacy confirmation-time proxy.
            completed = a.get("actual_completed_at") if "actual_completed_at" in a else a.get("completed_at")
        year = (effective.get("edition_year") or effective.get("year") or a.get("edition_year")
                or facts.get("edition_year") or facts.get("year")
                or definitions.get(a["challenge_id"], {}).get("year"))
        if year is None:
            # An orphaned import cannot be assigned a fabricated catalog year.
            # Reconciliation reports it for repair rather than breaking a clear.
            continue
        edition = effective.get("edition_id") or a.get("edition_id") or facts.get("edition_id") or a["challenge_id"]
        result.append({"id": a["id"], "tier": tier, "duration_group": duration,
                       "year": int(year), "edition_id": str(edition),
                       "completed_at": _date(completed)})
    # Unknown legacy unlock dates are not fabricated. Prefer dated alternatives
    # for a lifetime receipt when an earliest known completion exists.
    return sorted(result, key=lambda c: (c["completed_at"] is None,
                  c["completed_at"] or datetime.max.replace(tzinfo=timezone.utc), c["id"]))


def _qualification(roster, completions):
    requirements = json.loads(roster["requirements_json"])
    if not requirements:
        return None
    selected = []
    for edition in requirements:
        clear = next((c for c in completions if c["edition_id"] == str(edition)
                      and c["duration_group"] == "daily" and c["tier"] == roster["tier"]
                      and c["year"] == roster["year"]), None)
        if clear is None:
            return None
        selected.append(clear)
    unlock = (max(c["completed_at"] for c in selected).isoformat()
              if all(c["completed_at"] for c in selected) else None)
    return {"roster_id": roster["id"], "qualifying_attempts_json": _json([c["id"] for c in selected]),
            "unlocked_at": unlock}


def _record(conn, discord_id, duration, tier, year, qualification, *, operation_id=None, actor='system', reason='completion reconciliation'):
    old_row = conn.execute(
        "SELECT * FROM badge_receipts WHERE discord_id=? AND duration_group=? AND tier=? AND scope=?",
        (discord_id, duration, tier, f"year:{year}" if duration == "daily" else "lifetime"),
    ).fetchone()
    old = dict(old_row) if old_row else None
    if qualification is None and old is None:
        return
    if old is None:
        cur = conn.execute(
            "INSERT INTO badge_receipts(discord_id,duration_group,tier,scope,year,roster_id,"
            "qualifying_attempts_json,unlocked_at,issued_at,rule_version,status,requirements_json) VALUES (?,?,?,?,?,?,?,?,?,?,'active',?)",
            (discord_id, duration, tier, f"year:{year}" if duration == "daily" else "lifetime", year or None,
             qualification["roster_id"], qualification["qualifying_attempts_json"],
             qualification["unlocked_at"], utcnow_iso(), RULE_VERSION,
             _requirements(conn, qualification["roster_id"])),
        )
        receipt_id, event = cur.lastrowid, "issued"
    else:
        receipt_id = old["id"]
        changes = dict(qualification or {k: old[k] for k in ("roster_id", "qualifying_attempts_json", "unlocked_at")})
        changes["status"] = "active" if qualification is not None else "revoked"
        if all(old[k] == v for k, v in changes.items()):
            return
        event = ("revoked" if changes["status"] == "revoked" else "reinstated" if old["status"] == "revoked" else "requalified")
        conn.execute("UPDATE badge_receipts SET roster_id=?,qualifying_attempts_json=?,unlocked_at=?,status=?,requirements_json=?,revoked_at=? WHERE id=?",
                     (changes["roster_id"], changes["qualifying_attempts_json"], changes["unlocked_at"], changes["status"],
                      _requirements(conn, changes["roster_id"]), utcnow_iso() if event == "revoked" else None, receipt_id))
    after = dict(conn.execute("SELECT * FROM badge_receipts WHERE id=?", (receipt_id,)).fetchone())
    conn.execute("INSERT INTO badge_events(receipt_id,event_type,before_json,after_json,reason,actor,operation_id,effective_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (receipt_id, event, _json(old) if old else None, _json(after), reason, actor, operation_id, utcnow_iso(), utcnow_iso()))
    if duration == "daily":
        _sync_bonus(conn, after, operation_id=operation_id, actor=actor, reason=reason)


def _requirements(conn, roster_id):
    if roster_id is None:
        return None
    return conn.execute("SELECT requirements_json FROM daily_rosters WHERE id=?", (roster_id,)).fetchone()[0]


def _sync_bonus(conn, receipt, *, operation_id=None, actor='badges', reason='Daily sweep receipt reconciliation'):
    from app.services import scoring
    source = f"daily_bonus:{receipt['discord_id']}:{receipt['year']}:{receipt['tier']}"
    points = sweep_bonus(receipt["tier"]) if receipt["status"] == "active" else 0
    existing = conn.execute('SELECT * FROM award_ledger WHERE source_key=?', (source,)).fetchone()
    baseline_unknown = False
    if existing:
        baseline_unknown = _object(existing['metadata_json']).get('baseline_bonus_unknown', False)
    else:
        qualifying = json.loads(receipt['qualifying_attempts_json'])
        if qualifying:
            marks = ','.join('?' for _ in qualifying)
            sources = conn.execute(f'SELECT provenance FROM attempts WHERE id IN ({marks})', qualifying).fetchall()
            baseline_unknown = bool(sources) and all(r['provenance'] == 'legacy_baseline' for r in sources)
    if baseline_unknown:
        # The old scoreboard reconstructs only its latest year. Do not invent
        # additional historical bonuses merely because old clears survive.
        points = 0
    scoring.create_award(
        conn, source, receipt["discord_id"], points,
        badge_receipt_id=receipt["id"], edition_year=receipt["year"],
        award_type="daily_bonus", awarded_at=receipt["unlocked_at"],
        provenance='legacy_baseline_unknown_bonus' if baseline_unknown else RULE_VERSION,
        metadata={"roster_id": receipt["roster_id"], "baseline_bonus_unknown": baseline_unknown,
                  "issued_operation_id": operation_id},
    )
    scoring.set_award(conn, source, points,
                      operation_id=operation_id or f"badge-event:{conn.execute('SELECT MAX(id) FROM badge_events').fetchone()[0]}",
                      actor=actor, reason=reason)


def sync_user(conn: sqlite3.Connection, discord_id: str, *, operation_id=None, actor='system', reason='completion reconciliation', daily_scope=None, include_lifetime=True) -> None:
    """Reconcile all tiers/years, retaining history and one bonus entitlement.

    Existing Daily receipts first test their captured roster. Only a completion
    correction can invalidate that set; then try other published versions before
    revocation. New awards use only the latest prospective roster.
    """
    discord_id = str(discord_id)
    completions = _completions(conn, discord_id)
    rosters = {}
    all_rosters = [dict(r) for r in conn.execute('SELECT * FROM daily_rosters ORDER BY version DESC')]
    repairs = {r['supersedes_roster_id']: r for r in all_rosters if r['supersedes_roster_id'] is not None}
    for r in all_rosters:
        if r['id'] not in repairs:
            rosters.setdefault((r['year'], r['tier']), []).append(r)
    # New awards use the current catalog roster, following its explicit repair
    # chain, not whichever old historical roster happened to be repaired last.
    current = {}
    for r in all_rosters:
        key = (r['year'], r['tier'])
        if r['supersedes_roster_id'] is None and key not in current:
            while r['id'] in repairs:
                r = repairs[r['id']]
            current[key] = r
    for old in conn.execute("SELECT * FROM badge_receipts WHERE discord_id=? AND duration_group='daily'", (discord_id,)):
        rosters.setdefault((old["year"], old["tier"]), [])
    for (year, tier), versions in rosters.items():
        if daily_scope is not None and (year, tier) != daily_scope:
            continue
        old = conn.execute("SELECT * FROM badge_receipts WHERE discord_id=? AND duration_group='daily' AND tier=? AND year=?",
                           (discord_id, tier, year)).fetchone()
        candidates = [current[(year, tier)]] if (year, tier) in current else []
        if old:
            original = [r for r in versions if r["id"] == old["roster_id"]]
            candidates = original + [r for r in versions if r not in original]
        qualification = next((q for r in candidates if (q := _qualification(r, completions)) is not None), None)
        _record(conn, discord_id, "daily", tier, year, qualification, operation_id=operation_id, actor=actor, reason=reason)
    for duration in (("monthly", "yearly") if include_lifetime else ()):
        for tier in TIERS:
            clear = next((c for c in completions if c["duration_group"] == duration and c["tier"] == tier), None)
            qualification = ({"roster_id": None, "qualifying_attempts_json": _json([clear["id"]]),
                              "unlocked_at": clear["completed_at"].isoformat() if clear["completed_at"] else None}
                             if clear else None)
            _record(conn, discord_id, duration, tier, 0, qualification, operation_id=operation_id, actor=actor, reason=reason)


def _decorate(row):
    receipt = dict(row)
    receipt["glyph"] = GLYPHS[receipt["duration_group"]]
    receipt["active"] = receipt["status"] == "active"
    receipt["qualifying_attempts"] = json.loads(receipt["qualifying_attempts_json"])
    return receipt


def duration_badges(discord_id: str, year: int | None = None) -> list[dict]:
    """Highest valid 日/月/年 in header order, at most three; read-only."""
    year = datetime.now(timezone.utc).year if year is None else int(year)
    with db_session() as conn:
        receipts = [_decorate(r) for r in conn.execute(
            "SELECT * FROM badge_receipts WHERE discord_id=? AND status='active' AND (duration_group!='daily' OR year=?)",
            (str(discord_id), year))]
    return [max(matches, key=lambda r: TIERS.index(r["tier"])) for duration in GLYPHS
            if (matches := [r for r in receipts if r["duration_group"] == duration])]


def badge_history(discord_id: str) -> list[dict]:
    """All lower tiers, prior years and revoked receipts with append-only events."""
    with db_session() as conn:
        result = []
        for row in conn.execute("SELECT * FROM badge_receipts WHERE discord_id=? ORDER BY year, id", (str(discord_id),)):
            receipt = _decorate(row)
            receipt["events"] = [dict(e) for e in conn.execute("SELECT * FROM badge_events WHERE receipt_id=? ORDER BY id", (row["id"],))]
            result.append(receipt)
        return result
