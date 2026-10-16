"""Transactional scoring receipts and compensating, previewed policy operations.

All helpers taking ``conn`` run in the caller's transaction and never commit.
Signed ledger deltas intentionally do not use db.sql_int's nonnegative clamp.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.db import db_session, utcnow_iso

BASE_POINTS = {'easy': 1, 'medium': 2, 'hard': 3, 'ota_master': 4}
MULTIPLIERS = {'daily': 1, 'weekly': 2, 'monthly': 3, 'yearly': 4, 'special': 1}
LEGACY_MULTIPLIERS = {**MULTIPLIERS, 'monthly': 10, 'yearly': 50}
FORMULA_VERSION = 'compressed_long_v1'


def _merge_policy(target, revision):
    for field in ('base_points', 'multipliers'):
        target[field].update(revision.get(field, {}))
    if 'formula_version' in revision:
        target['formula_version'] = revision['formula_version']


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _load(value, default=None):
    return json.loads(value) if value else ({} if default is None else default)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _points(value):
    if isinstance(value, bool) or not isinstance(value, int) or not -(2**63) < value < 2**63:
        raise ValueError('points must be a signed SQLite integer')
    return value


def _catalog_records(conn):
    # Lazy integration: lifecycle owns catalog identity/revision backfill.
    try:
        from app.services.lifecycle import ensure_catalog_records
    except ImportError:
        return
    ensure_catalog_records(conn)


def _instant(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('policy timestamps must be timezone-aware')
    return dt.astimezone(timezone.utc)


def current_policy(conn, challenge=None, *, at=None, through_id=None) -> dict:
    cutoff = _instant(at)
    result = {'base_points': dict(BASE_POINTS), 'multipliers': dict(LEGACY_MULTIPLIERS), 'formula_version': 'linear_v1'}
    rows = [row for row in conn.execute('SELECT id,policy_json,scope_json,effective_at FROM scoring_policies ORDER BY id')
            if (through_id is None or row['id'] <= through_id)
            and (not row['effective_at'] or _instant(row['effective_at']) <= cutoff)]
    # A compensating revision withdraws the original formula at its own cutover.
    # Replaying the remaining revisions restores each challenge's applicable
    # baseline even when prior edition/identity/year scopes overlap differently.
    # Filtering first retains the original formula for historical snapshots.
    reverted = {_load(row['policy_json'])['reverts_policy_id'] for row in rows
                if 'reverts_policy_id' in _load(row['policy_json'])}
    for row in rows:
        if row['id'] in reverted:
            continue
        scope = _load(row['scope_json'])
        scoped = any(k in scope for k in ('edition_ids', 'identity_ids', 'years'))
        if scoped and (challenge is None or not _scope_match(challenge, scope)):
            continue
        policy = _load(row['policy_json'])
        _merge_policy(result, policy)
    return result


def points_for_challenge(challenge: dict, policy: dict | None = None) -> int:
    """Resolve an accepted rules snapshot; points=None means inheritance."""
    policy = policy or {'base_points': BASE_POINTS, 'multipliers': MULTIPLIERS, 'formula_version': FORMULA_VERSION}
    override = challenge.get('points')
    if override is not None:
        return _points(int(override))
    tier = str(challenge.get('tier', '')).lower()
    group = challenge.get('duration_group') or ('special' if tier == 'special' else 'daily')
    base = policy.get('base_points', BASE_POINTS).get(tier, 0)
    multiplier = policy.get('multipliers', MULTIPLIERS).get(group, 1)
    if policy.get('formula_version') == FORMULA_VERSION and group in ('weekly', 'monthly', 'yearly'):
        return _points(((base + 1) * multiplier + 1) // 2)
    return _points(base * multiplier)


def reward_points(conn, challenge: dict | None) -> int:
    """Read-only catalog/display formula using current global and scoped revisions.

    No catalog import, writes, or synchronization: safe for progress denominators.
    """
    if not challenge:
        return 0
    ch = dict(challenge)
    ch['edition_id'] = ch.get('edition_id') or ch.get('id')
    ch['identity_id'] = ch.get('identity_id') or ch.get('id')
    return points_for_challenge(ch, current_policy(conn, ch))


def capture_snapshot(conn, challenge: dict, *, at=None) -> dict:
    """Call at actual start; persist returned dict as scoring_snapshot_json."""
    challenge = dict(challenge)
    challenge['edition_id'] = challenge.get('edition_id') or challenge.get('id')
    challenge['identity_id'] = challenge.get('identity_id') or challenge.get('id')
    policy = current_policy(conn, challenge, at=at)
    policy_id = None
    cutoff = _instant(at)
    if at is not None:
        # A restart may activate a scheduled row after subsequent override edits.
        # Rewind those audited edits to its actual scheduled start instant.
        for revision in conn.execute('SELECT p.effective_at,o.payload_json,o.operation_type FROM scoring_policies p JOIN admin_operations o ON o.operation_id=p.operation_id ORDER BY p.id DESC'):
            if revision['effective_at'] and _instant(revision['effective_at']) > cutoff:
                payload = _load(revision['payload_json'])
                reverting = revision['operation_type'] == 'scoring_revert'
                if reverting:
                    original = conn.execute('SELECT payload_json FROM admin_operations WHERE operation_id=?', (payload['operation_id'],)).fetchone()
                    payload = _load(original['payload_json'])
                for change in payload.get('definitions', []):
                    if change['id'] == challenge.get('id'):
                        challenge['points'] = change['new_points' if reverting else 'old_points']
    for row in conn.execute('SELECT id,scope_json,effective_at FROM scoring_policies ORDER BY id'):
        scope = _load(row['scope_json'])
        if (not row['effective_at'] or _instant(row['effective_at']) <= cutoff) and _scope_match(challenge, scope):
            policy_id = row['id']
    return {'points': points_for_challenge(challenge, policy), 'policy_id': policy_id,
            'policy': policy, 'points_override': challenge.get('points')}


def create_award(conn, source_key, discord_id, points, *, attempt_id=None,
                 badge_receipt_id=None, identity_id=None, edition_id=None, edition_year=None,
                 award_type='completion', policy_id=None, awarded_at=None,
                 provenance='receipt', metadata=None):
    """Insert once; use set_award for later corrections. Returns effective points."""
    points = _points(points)
    conn.execute('''INSERT OR IGNORE INTO award_ledger
        (source_key,discord_id,attempt_id,badge_receipt_id,identity_id,edition_id,edition_year,
         award_type,original_points,effective_points,original_policy_id,current_policy_id,
         original_awarded_at,recorded_at,provenance,metadata_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (source_key, str(discord_id), attempt_id, badge_receipt_id, identity_id, edition_id,
         edition_year, award_type, points, points, policy_id, policy_id, awarded_at,
         utcnow_iso(), provenance, _json(metadata or {})))
    return int(conn.execute('SELECT effective_points FROM award_ledger WHERE source_key=?',
                            (source_key,)).fetchone()[0])


def set_award(conn, source_key, points, *, operation_id, actor, reason):
    points = _points(points)
    row = conn.execute('SELECT * FROM award_ledger WHERE source_key=?', (source_key,)).fetchone()
    if not row:
        raise ValueError(f'unknown award source: {source_key}')
    previous = conn.execute('SELECT new_points FROM point_adjustments WHERE operation_id=? AND source_key=?',
                            (operation_id, source_key)).fetchone()
    if previous:
        if previous[0] != points:
            raise ValueError('operation ID reused with different points')
        return points
    old = row['effective_points']
    if old == points:
        return points
    delta = _points(points - old)
    conn.execute('''INSERT INTO point_adjustments
        (source_key,old_points,new_points,delta,operation_id,actor,reason,effective_at,recorded_at)
        VALUES(?,?,?,?,?,?,?,?,?)''',
        (source_key, old, points, delta, operation_id, str(actor), reason, utcnow_iso(), utcnow_iso()))
    conn.execute('UPDATE award_ledger SET effective_points=?,revision=revision+1 WHERE source_key=?',
                 (points, source_key))
    return points


def ensure_completion_award(conn, attempt_id, *, actor='system', reason=None, operation_id=None):
    row = conn.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone()
    if not row:
        raise ValueError('unknown attempt')
    a = dict(row)
    key = f'completion:{attempt_id}'
    receipt = conn.execute('SELECT * FROM award_ledger WHERE source_key=?', (key,)).fetchone()
    if a['status'] != 'completed':
        if receipt:
            metadata = _load(receipt['metadata_json'])
            if not metadata.get('voided'):
                metadata.update(voided=True, before_void=receipt['effective_points'])
                conn.execute('UPDATE award_ledger SET metadata_json=? WHERE source_key=?', (_json(metadata), key))
            set_award(conn, key, 0, operation_id=operation_id or f"completion-state:{attempt_id}:{a['correction_revision']}:{receipt['revision']}:void",
                      actor=actor, reason=reason or 'completion no longer valid')
        return 0
    ch = dict(conn.execute('SELECT * FROM challenge_defs WHERE id=?', (a['challenge_id'],)).fetchone())
    rules = _load(a.get('rules_snapshot_json'))
    facts = {**rules, **_load(a.get('effective_completion_json'))}
    ch.update({k: facts[k] for k in ('tier', 'duration_group', 'category') if facts.get(k) is not None})
    snap = _load(a.get('scoring_snapshot_json'))
    if receipt:
        metadata = _load(receipt['metadata_json'])
        if metadata.get('voided'):
            target = metadata['before_void']
            set_award(conn, key, target, operation_id=operation_id or f"reinstate:{attempt_id}:{receipt['revision']}",
                      actor=actor, reason=reason or 'completion reinstated')
            metadata.pop('voided', None)
            conn.execute('UPDATE award_ledger SET metadata_json=? WHERE source_key=?', (_json(metadata), key))
        return conn.execute('SELECT effective_points FROM award_ledger WHERE source_key=?', (key,)).fetchone()[0]
    legacy = a.get('provenance') == 'legacy_baseline' or not snap
    # Missing historical snapshots retain the release's migration baseline,
    # independent of later prospective product defaults.
    points = snap.get('points', points_for_challenge(ch, {'base_points': BASE_POINTS, 'multipliers': LEGACY_MULTIPLIERS}))
    classification_correction = None
    if snap and any(facts.get(k) != rules.get(k) for k in ('tier', 'duration_group')):
        # A deliberate correction can precede completion (including scheduled
        # activation). Resolve its effective classification under the accepted
        # policy/absolute override without rewriting the original rule snapshot.
        corrected_rules = {**ch, 'points': snap.get('points_override')}
        points = points_for_challenge(corrected_rules, snap.get('policy'))
        from app.services.lifecycle import effective_rules
        for event in conn.execute("SELECT * FROM attempt_events WHERE attempt_id=? AND event_type='correction' ORDER BY id DESC", (attempt_id,)):
            old, new = effective_rules(_load(event['before_json'])), effective_rules(_load(event['after_json']))
            if any(old.get(k) != new.get(k) for k in ('tier', 'duration_group')):
                classification_correction = {'operation_id': event['operation_id'], 'actor': event['actor'],
                                             'reason': event['reason'], 'accepted_points': snap.get('points'),
                                             'effective_points': points}
                break
    return create_award(conn, key, a['discord_id'], points, attempt_id=attempt_id,
                        identity_id=a.get('identity_id') or ch.get('identity_id'),
                        edition_id=a.get('edition_id') or ch.get('edition_id') or ch['id'],
                        edition_year=a.get('edition_year') or ch['year'], policy_id=snap.get('policy_id'),
                        awarded_at=a.get('actual_completed_at') or a.get('completed_at'),
                        provenance='legacy_baseline' if legacy else 'attempt_snapshot',
                        metadata={'tier': ch['tier'], 'duration_group': ch['duration_group'],
                                  'points_override': snap.get('points_override', ch.get('points')),
                                  'legacy_repeat_exception': legacy,
                                  'verification_method': a.get('verify_method'),
                                  'administrative_exception': str(a.get('verify_method') or '').startswith('test_') or a.get('verify_method') == 'admin_attestation',
                                  'issued_operation_id': operation_id, 'actor': actor, 'reason': reason,
                                  'classification_correction': classification_correction})


def sync_user(conn, discord_id, *, actor='system', reason=None, operation_id=None):
    """Baseline direct inserts and reconcile void/reinstate without resetting rescoring."""
    for a in conn.execute('SELECT id FROM attempts WHERE discord_id=?', (str(discord_id),)).fetchall():
        ensure_completion_award(conn, a['id'], actor=actor, reason=reason, operation_id=operation_id)


def user_points(conn, discord_id):
    sync_user(conn, discord_id)
    return int(conn.execute('SELECT COALESCE(SUM(effective_points),0) FROM award_ledger WHERE discord_id=?',
                            (str(discord_id),)).fetchone()[0])


def upgrade_duration_defaults(conn):
    """One audited cutover including open attempts; completed awards stay intact."""
    operation_id = 'duration-defaults-compressed-v1'
    if conn.execute('SELECT 1 FROM meta WHERE key=?', (operation_id,)).fetchone():
        return
    # Preserve a pre-cutover baseline for partially captured active attempts.
    for row in conn.execute("SELECT a.id AS attempt_id,c.* FROM attempts a JOIN challenge_defs c ON c.id=a.challenge_id "
                            "WHERE a.status='active' AND a.scoring_snapshot_json IS NULL").fetchall():
        challenge = dict(row)
        attempt_id = challenge.pop('attempt_id')
        conn.execute('UPDATE attempts SET scoring_snapshot_json=? WHERE id=?',
                     (_json(capture_snapshot(conn, challenge)), attempt_id))
    policy = {'multipliers': {key: MULTIPLIERS[key] for key in ('weekly', 'monthly', 'yearly')},
              'formula_version': FORMULA_VERSION}
    # Reuse classification/override-aware preview calculation, then deliberately
    # apply only its open snapshots. Completed awards are never recalculated here.
    preview = _preview(conn, policy, {'retroactive': True}, True, False)
    opens = []
    for change in preview['open_attempts']:
        row = conn.execute('SELECT a.rules_snapshot_json,a.effective_completion_json,c.duration_group '
                           'FROM attempts a JOIN challenge_defs c ON c.id=a.challenge_id WHERE a.id=?',
                           (change['attempt_id'],)).fetchone()
        accepted = {**_load(row['rules_snapshot_json']), **_load(row['effective_completion_json'])}
        if accepted.get('duration_group', row['duration_group']) in ('weekly', 'monthly', 'yearly'):
            opens.append(change)
    recorded_at = utcnow_iso()
    reason = 'Compressed Weekly/Monthly/Yearly scoring, including open attempts; completed awards retained'
    previous = current_policy(conn)
    cursor = conn.execute('INSERT INTO scoring_policies(policy_json,effective_at,recorded_at,actor,reason,operation_id) VALUES(?,?,?,?,?,?)',
                          (_json(policy), recorded_at, recorded_at, 'migration', reason, operation_id))
    for change in opens:
        snapshot = {**change['new_snapshot'], 'policy_id': cursor.lastrowid,
                    'explicit_scoring_revision': {'operation_id': operation_id, 'effective_at': recorded_at,
                                                 'actor': 'migration', 'reason': reason}}
        conn.execute('UPDATE attempts SET scoring_snapshot_json=? WHERE id=?', (_json(snapshot), change['attempt_id']))
    _record(conn, operation_id, 'duration_defaults', {'previous_policy': previous, 'policy': policy, 'open_attempts': opens},
            {'policy_id': cursor.lastrowid, 'open_attempts': len(opens)}, 'migration', reason)
    conn.execute('INSERT INTO meta(key,value) VALUES(?,?)', (operation_id, recorded_at))


def bootstrap(conn):
    if not conn.execute('SELECT 1 FROM scoring_policies LIMIT 1').fetchone():
        conn.execute('INSERT INTO scoring_policies(policy_json,recorded_at,actor,reason) VALUES(?,?,?,?)',
                     (_json({'base_points': BASE_POINTS, 'multipliers': MULTIPLIERS, 'formula_version': FORMULA_VERSION}), utcnow_iso(),
                      'migration', 'initial defaults'))
    for row in conn.execute('SELECT DISTINCT discord_id FROM attempts').fetchall():
        sync_user(conn, row[0])
    if conn.execute("SELECT 1 FROM meta WHERE key='scoring_baseline_v1'").fetchone():
        return
    # Reproduce the old scoreboard: every completed attempt counts, including repeats;
    # sweep bonuses were computed only for MAX(catalog year), including Physical rows.
    from app.catalog import sweep_bonus
    year = conn.execute('SELECT MAX(year) FROM challenge_defs').fetchone()[0]
    if year is not None:
        for tier in BASE_POINTS:
            required = {r[0] for r in conn.execute('SELECT id FROM challenge_defs WHERE year=? AND LOWER(tier)=?', (year, tier))}
            if not required:
                continue
            for user in conn.execute("SELECT DISTINCT discord_id FROM attempts WHERE status='completed'").fetchall():
                done = {r[0] for r in conn.execute("SELECT challenge_id FROM attempts WHERE discord_id=? AND status='completed'", (user[0],))}
                if required <= done:
                    create_award(conn, f'daily_bonus:{user[0]}:{year}:{tier}', user[0], sweep_bonus(tier),
                                 edition_year=year, award_type='daily_bonus', provenance='legacy_baseline',
                                 metadata={'tier': tier, 'requirements': sorted(required), 'reconstruction': 'current_catalog_only'})
    conn.execute("INSERT INTO meta(key,value) VALUES('scoring_baseline_v1',?)", (utcnow_iso(),))


def _validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) - {'base_points', 'multipliers', 'points_override', 'formula_version'}:
        raise ValueError('unsupported policy fields')
    if 'formula_version' in policy and policy['formula_version'] not in ('linear_v1', FORMULA_VERSION):
        raise ValueError('invalid scoring formula version')
    for field, allowed in (('base_points', BASE_POINTS), ('multipliers', MULTIPLIERS)):
        for key, value in policy.get(field, {}).items():
            if key not in allowed or _points(value) < 0:
                raise ValueError('invalid scoring policy')
    if policy.get('points_override') is not None and 'points_override' in policy:
        if _points(policy['points_override']) < 0:
            raise ValueError('negative override')


def _scope_match(ch, scope):
    for field, key in (('edition_ids', 'edition_id'), ('identity_ids', 'identity_id'), ('years', 'year')):
        if field in scope and ch.get(key) not in scope[field]:
            return False
    return True


def _date_match(a, scope):
    field = scope.get('date_field', 'started_at')
    value = a.get('actual_completed_at') if field == 'completed_at' else a.get(field)
    # Unknown actual completion is explicitly excluded, never replaced with confirmation.
    for key in ('date_from', 'date_until'):
        if scope.get(key):
            if not value:
                return False
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            boundary = datetime.fromisoformat(scope[key].replace('Z', '+00:00'))
            if dt.tzinfo is None or boundary.tzinfo is None:
                raise ValueError('date scope requires timezone-aware timestamps')
            if (key == 'date_from' and dt < boundary) or (key == 'date_until' and dt >= boundary):
                return False
    return True


def _state(conn):
    # Conservative whole-backend fingerprint: no stale roster/source/rule edits pass.
    tables = ('challenge_defs', 'attempts', 'award_ledger', 'scoring_policies', 'point_adjustments',
              'badge_receipts', 'daily_rosters', 'admin_operations')
    return _digest({t: [dict(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in tables})


def _preview(conn, policy, scope, include_open, include_overrides):
    _validate_policy(policy)
    scope = dict(scope or {})
    allowed = {'edition_ids','identity_ids','years','date_field','date_from','date_until','retroactive'}
    if set(scope) - allowed or scope.get('date_field', 'started_at') not in ('started_at','completed_at'):
        raise ValueError('unsupported scope or date field')
    if 'points_override' in policy and not (scope.get('edition_ids') or scope.get('identity_ids')):
        raise ValueError('edition override requires explicit edition/identity scope')
    _catalog_records(conn)
    for row in conn.execute('SELECT DISTINCT discord_id FROM attempts').fetchall():
        sync_user(conn, row[0])
    merged = current_policy(conn)
    _merge_policy(merged, policy)
    changes, definitions, opens, excluded = [], [], [], []
    selected = {}
    for row in conn.execute('SELECT * FROM challenge_defs').fetchall():
        ch = dict(row)
        ch['edition_id'] = ch.get('edition_id') or ch['id']
        ch['identity_id'] = ch.get('identity_id') or ch['id']
        if not _scope_match(ch, scope):
            continue
        if ch['points'] is not None and not include_overrides and 'points_override' not in policy:
            excluded.append(ch['id'])
            continue
        if 'points_override' in policy:
            definitions.append({'id': ch['id'], 'old_points': ch['points'], 'new_points': policy['points_override']})
            ch['points'] = policy['points_override']
        selected[ch['id']] = ch
    if scope.get('retroactive', True):
        for row in conn.execute('SELECT * FROM attempts').fetchall():
            a = dict(row)
            ch = selected.get(a['challenge_id'])
            if not ch or not _date_match(a, scope):
                continue
            # Historical classification comes from receipt, not mutable catalog.
            receipt = conn.execute('SELECT * FROM award_ledger WHERE source_key=?', (f"completion:{a['id']}",)).fetchone()
            historical = dict(ch)
            if receipt:
                metadata = _load(receipt['metadata_json'])
                for k in ('tier','duration_group'):
                    if k in metadata:
                        historical[k] = metadata[k]
                if 'points_override' not in policy:
                    historical['points'] = metadata.get('points_override')
                    if historical['points'] is not None and not include_overrides:
                        continue
            elif a['status'] in ('active', 'scheduled'):
                accepted = {**_load(a.get('rules_snapshot_json')), **_load(a.get('effective_completion_json'))}
                historical.update({key: accepted[key] for key in ('tier', 'duration_group') if accepted.get(key) is not None})
                if 'points_override' not in policy:
                    snapshot = _load(a.get('scoring_snapshot_json'))
                    historical['points'] = snapshot.get('points_override', historical['points'])
                    if historical['points'] is not None and not include_overrides:
                        continue
            replacement = current_policy(conn, ch)
            _merge_policy(replacement, policy)
            target = points_for_challenge(historical, replacement)
            if a['status'] == 'completed' and receipt and (
                target != receipt['effective_points'] or (
                    'points_override' in policy and metadata.get('points_override') != policy['points_override']
                )
            ):
                changes.append({'source_key': receipt['source_key'], 'discord_id': a['discord_id'],
                                'old_points': receipt['effective_points'], 'new_points': target,
                                'delta': target - receipt['effective_points'],
                                'old_policy_id': receipt['current_policy_id'], 'old_metadata': metadata})
            elif include_open and a['status'] in ('active', 'scheduled'):
                opens.append({'attempt_id': a['id'], 'old_snapshot': a['scoring_snapshot_json'],
                              'new_snapshot': {'points': target, 'policy': replacement, 'points_override': historical['points']}})
    totals = {}
    for change in changes:
        did = change['discord_id']
        if did not in totals:
            old = conn.execute('SELECT COALESCE(SUM(effective_points),0) FROM award_ledger WHERE discord_id=?', (did,)).fetchone()[0]
            totals[did] = {'old_points': old, 'new_points': old, 'delta': 0}
        totals[did]['new_points'] += change['delta']
        totals[did]['delta'] += change['delta']
    result = {'policy': policy, 'scope': scope, 'include_open': include_open, 'include_overrides': include_overrides,
              'merged_policy': merged, 'changes': changes, 'definitions': definitions, 'open_attempts': opens,
              'excluded_overrides': excluded, 'totals': totals, 'state_version': _state(conn)}
    result['preview_version'] = _digest(result)
    return result


def preview_recalculation(policy, scope=None, *, include_open=False, include_overrides=False):
    with db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return _preview(conn, policy, scope, include_open, include_overrides)


def _existing(conn, operation_id, payload):
    row = conn.execute('SELECT * FROM admin_operations WHERE operation_id=?', (operation_id,)).fetchone()
    if row:
        if row['payload_json'] != _json(payload):
            raise ValueError('operation ID reused for a different request')
        return _load(row['result_json'])
    return None


def _record(conn, operation_id, kind, payload, result, actor, reason, reverts=None):
    conn.execute('''INSERT INTO admin_operations
        (operation_id,operation_type,payload_json,result_json,actor,reason,recorded_at,reverts_operation_id)
        VALUES(?,?,?,?,?,?,?,?)''', (operation_id, kind, _json(payload), _json(result), str(actor), reason, utcnow_iso(), reverts))
    # Consumers use this generation to invalidate point denominators/profile caches.
    conn.execute("INSERT INTO meta(key,value) VALUES('scoring_generation', '1') "
                 "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)")


def apply_recalculation(preview, *, actor, reason, operation_id):
    if not actor or not reason or not operation_id:
        raise ValueError('actor, reason and operation ID required')
    with db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = _existing(conn, operation_id, preview)
        if existing is not None:
            return existing
        fresh = _preview(conn, preview['policy'], preview['scope'], preview['include_open'], preview['include_overrides'])
        if fresh != preview:
            raise ValueError('stale or modified scoring preview; preview again')
        # Partial revisions compose in order. Scoped policies affect only matching
        # future starts; unrelated defaults and accepted open snapshots stay intact.
        policy_value = {k: v for k, v in preview['policy'].items() if k in ('base_points', 'multipliers', 'formula_version')}
        cursor = conn.execute('''INSERT INTO scoring_policies
            (policy_json,scope_json,effective_at,recorded_at,actor,reason,operation_id) VALUES(?,?,?,?,?,?,?)''',
            (_json(policy_value), _json(preview['scope']), utcnow_iso(), utcnow_iso(), str(actor), reason, operation_id))
        policy_id = cursor.lastrowid
        for d in preview['definitions']:
            conn.execute('UPDATE challenge_defs SET points=? WHERE id=?', (d['new_points'], d['id']))
        for change in preview['changes']:
            set_award(conn, change['source_key'], change['new_points'], operation_id=operation_id, actor=actor, reason=reason)
            receipt = conn.execute('SELECT metadata_json FROM award_ledger WHERE source_key=?', (change['source_key'],)).fetchone()
            metadata = _load(receipt['metadata_json'])
            metadata.setdefault('original_metadata', dict(metadata))
            if 'points_override' in preview['policy']:
                metadata['points_override'] = preview['policy']['points_override']
            conn.execute('UPDATE award_ledger SET current_policy_id=?,metadata_json=? WHERE source_key=?',
                         (policy_id, _json(metadata), change['source_key']))
        applied_open = {}
        effective_at = conn.execute('SELECT effective_at FROM scoring_policies WHERE id=?', (policy_id,)).fetchone()[0]
        for a in preview['open_attempts']:
            snapshot = {**a['new_snapshot'], 'policy_id': policy_id,
                        'explicit_scoring_revision': {'operation_id': operation_id, 'effective_at': effective_at,
                                                      'actor': str(actor), 'reason': reason}}
            applied_open[str(a['attempt_id'])] = snapshot
            conn.execute('UPDATE attempts SET scoring_snapshot_json=? WHERE id=?', (_json(snapshot), a['attempt_id']))
        result = {'operation_id': operation_id, 'adjusted': len(preview['changes']), 'totals': preview['totals'],
                  'policy_id': policy_id, 'open_snapshots': applied_open}
        _record(conn, operation_id, 'scoring_recalculation', preview, result, actor, reason)
        return result


def revert_operation(operation_id, *, actor, reason, revert_operation_id):
    if not actor or not reason or not revert_operation_id or operation_id == revert_operation_id:
        raise ValueError('actor, reason and distinct revert operation ID required')
    payload = {'operation_id': operation_id}
    with db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = _existing(conn, revert_operation_id, payload)
        if existing is not None:
            return existing
        original = conn.execute('SELECT * FROM admin_operations WHERE operation_id=?', (operation_id,)).fetchone()
        if not original or original['operation_type'] != 'scoring_recalculation':
            raise ValueError('unknown scoring operation')
        if conn.execute('SELECT 1 FROM admin_operations WHERE reverts_operation_id=?', (operation_id,)).fetchone():
            raise ValueError('operation has already been reverted')
        preview = _load(original['payload_json'])
        # Compensation subtracts the original delta from the current effective award;
        # intervening operations are retained instead of restoring cached totals.
        adjusted = []
        for change in conn.execute('SELECT * FROM point_adjustments WHERE operation_id=?', (operation_id,)).fetchall():
            receipt = conn.execute('SELECT * FROM award_ledger WHERE source_key=?', (change['source_key'],)).fetchone()
            metadata = _load(receipt['metadata_json'])
            if metadata.get('voided'):
                raise ValueError('cannot revert a now-void source; correct/repreview explicitly')
            target = receipt['effective_points'] - change['delta']
            set_award(conn, change['source_key'], target, operation_id=revert_operation_id, actor=actor, reason=reason)
            adjusted.append({'source_key': change['source_key'], 'new_points': target})
        for source in preview['changes']:
            receipt = conn.execute('SELECT current_policy_id,metadata_json FROM award_ledger WHERE source_key=?', (source['source_key'],)).fetchone()
            if receipt['current_policy_id'] != _load(original['result_json'])['policy_id']:
                raise ValueError('source scoring policy changed since operation; explicit new preview required')
            metadata = _load(receipt['metadata_json'])
            if 'points_override' in preview['policy']:
                metadata['points_override'] = source.get('old_metadata', {}).get('points_override')
            # Compensate policy fields only; intervening semantic corrections
            # (tier/duration/category) are not reverted by a point operation.
            conn.execute('UPDATE award_ledger SET current_policy_id=?,metadata_json=? WHERE source_key=?',
                         (source.get('old_policy_id'), _json(metadata), source['source_key']))
        for d in preview['definitions']:
            current = conn.execute('SELECT points FROM challenge_defs WHERE id=?', (d['id'],)).fetchone()
            if not current or current[0] != d['new_points']:
                raise ValueError('edition policy changed since operation; explicit new preview required')
            conn.execute('UPDATE challenge_defs SET points=? WHERE id=?', (d['old_points'], d['id']))
        for a in preview['open_attempts']:
            current = conn.execute('SELECT scoring_snapshot_json,status FROM attempts WHERE id=?', (a['attempt_id'],)).fetchone()
            expected_snapshot = _load(original['result_json']).get('open_snapshots', {}).get(str(a['attempt_id']), a['new_snapshot'])
            if not current or current[1] not in ('active','scheduled') or current[0] != _json(expected_snapshot):
                raise ValueError('open attempt changed since operation')
            conn.execute('UPDATE attempts SET scoring_snapshot_json=? WHERE id=?', (a['old_snapshot'], a['attempt_id']))
        # Revert only changed fields. Reject later overlapping formula revisions.
        original_policy_id = _load(original['result_json'])['policy_id']
        for field in ('base_points', 'multipliers'):
            keys = preview['policy'].get(field, {})
            if not keys:
                continue
            for later in conn.execute('SELECT policy_json FROM scoring_policies WHERE id>?', (original_policy_id,)):
                if 'formula_version' in _load(later[0]) or set(_load(later[0]).get(field, {})) & set(keys):
                    raise ValueError('policy changed since operation; explicit new preview required')
        if 'formula_version' in preview['policy']:
            for later in conn.execute('SELECT policy_json FROM scoring_policies WHERE id>?', (original_policy_id,)):
                if set(_load(later[0])) & {'formula_version', 'base_points', 'multipliers'}:
                    raise ValueError('policy changed since operation; explicit new preview required')
        # Do not flatten heterogeneous prior scopes into a single replacement.
        # This internal marker is append-only and cannot be supplied as a policy
        # through preview_recalculation's public validation.
        policy = {'reverts_policy_id': original_policy_id}
        conn.execute('INSERT INTO scoring_policies(policy_json,scope_json,effective_at,recorded_at,actor,reason,operation_id) VALUES(?,?,?,?,?,?,?)',
                     (_json(policy), _json(preview['scope']), utcnow_iso(), utcnow_iso(), str(actor), reason, revert_operation_id))
        result = {'operation_id': revert_operation_id, 'reverts': operation_id, 'adjusted': adjusted}
        _record(conn, revert_operation_id, 'scoring_revert', payload, result, actor, reason, operation_id)
        return result
