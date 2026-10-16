"""Explicit, previewed repair of immutable historical Daily sweep rosters.

A repair appends a replacement version and a supersession edge. It never edits
old requirements or deletes an award receipt. All affected members are recomputed
in one transaction, including alternative applicable rosters and signed bonuses.
Previews run the actual synchronizers inside a rolled-back savepoint.
"""
from __future__ import annotations

import hashlib
import json

from app.db import db_session, utcnow_iso
from app.services import badges, scoring
from app.services.lifecycle import _fingerprint, _operation, _reason, _record_operation


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _validate(conn, roster_id, requirements):
    if not isinstance(roster_id, int) or isinstance(roster_id, bool):
        raise ValueError('Roster ID must be an integer')
    row = conn.execute('SELECT * FROM daily_rosters WHERE id=?', (roster_id,)).fetchone()
    if not row:
        raise ValueError('Unknown roster')
    if conn.execute('SELECT 1 FROM daily_rosters WHERE supersedes_roster_id=?', (roster_id,)).fetchone():
        raise ValueError('Roster has already been repaired; select its replacement version')
    if not isinstance(requirements, list) or len(requirements) > 1000 or any(not isinstance(x, str) or not x.strip() for x in requirements):
        raise ValueError('Requirements must be a list of at most 1000 nonempty edition IDs')
    if len(set(requirements)) != len(requirements):
        raise ValueError('Duplicate roster requirement')
    old = dict(row)
    required = sorted(requirements)
    if required == sorted(json.loads(old['requirements_json'])):
        raise ValueError('Roster requirements are unchanged')
    warnings = []
    for edition in required:
        record = conn.execute('SELECT c.* FROM challenge_editions e JOIN challenge_defs c ON c.id=e.challenge_id WHERE e.id=? AND e.year=?', (edition, old['year'])).fetchone()
        if record is None:
            raise ValueError(f'Unknown edition or wrong catalog year: {edition}')
        definition = dict(record)
        compatible = definition['duration_group'] == 'daily' and definition['tier'] == old['tier']
        if not compatible:
            # A historical edition may have been reclassified prospectively.
            for revision in conn.execute('SELECT snapshot_json FROM definition_revisions WHERE edition_id=?', (edition,)):
                fact = json.loads(revision['snapshot_json'] or '{}')
                if fact.get('duration_group') == 'daily' and fact.get('tier') == old['tier']:
                    compatible = True
                    break
        if not compatible:
            raise ValueError(f'Edition was never a Daily challenge in this tier: {edition}')
        if definition['lifecycle_state'] == 'retired':
            warnings.append(f'Retired edition retained as a deliberate historical requirement: {edition}')
    if not required:
        warnings.append('Empty roster: it cannot qualify for a Daily badge or bonus')
    return old, required, warnings


def _members(conn, year, tier):
    # Include unawarded progress, revoked receipts and legacy clears with no
    # edition_year. Not just currently active members or existing receipt owners.
    return [r[0] for r in conn.execute('''
        SELECT discord_id FROM badge_receipts WHERE duration_group='daily' AND year=? AND tier=?
        UNION SELECT a.discord_id FROM attempts a JOIN challenge_defs c ON c.id=a.challenge_id
        WHERE COALESCE(a.edition_year,c.year)=? AND a.status='completed'
        ORDER BY discord_id''', (year, tier, year))]


def _current_roster(conn, year, tier):
    row = conn.execute('SELECT * FROM daily_rosters WHERE year=? AND tier=? AND supersedes_roster_id IS NULL ORDER BY version DESC LIMIT 1', (year, tier)).fetchone()
    while row:
        replacement = conn.execute('SELECT * FROM daily_rosters WHERE supersedes_roster_id=?', (row['id'],)).fetchone()
        if replacement is None:
            return row
        row = replacement
    return None


def _state(conn, members, year, tier):
    result = {}
    for member in members:
        receipt = conn.execute("SELECT * FROM badge_receipts WHERE discord_id=? AND duration_group='daily' AND year=? AND tier=?", (member, year, tier)).fetchone()
        roster = None
        if receipt and receipt['status'] == 'active':
            roster = conn.execute('SELECT * FROM daily_rosters WHERE id=?', (receipt['roster_id'],)).fetchone()
        if roster is None:
            roster = _current_roster(conn, year, tier)
        required = json.loads(roster['requirements_json']) if roster else []
        done = {c['edition_id'] for c in badges._completions(conn, member)
                if c['year'] == year and c['tier'] == tier and c['duration_group'] == 'daily'}
        result[member] = {
            'points': conn.execute('SELECT COALESCE(SUM(effective_points),0) FROM award_ledger WHERE discord_id=?', (member,)).fetchone()[0],
            'badge': {k: receipt[k] for k in ('status', 'roster_id', 'qualifying_attempts_json', 'unlocked_at')} if receipt else None,
            'progress': {'roster_id': roster['id'] if roster else None, 'required': required,
                         'completed': sorted(done & set(required)), 'missing': sorted(set(required) - done)},
        }
    return result


def _append(conn, old, requirements, actor, reason):
    version = conn.execute('SELECT COALESCE(MAX(version),0)+1 FROM daily_rosters WHERE year=? AND tier=?', (old['year'], old['tier'])).fetchone()[0]
    return conn.execute('''INSERT INTO daily_rosters
        (year,tier,version,requirements_json,published_at,recorded_at,actor,reason,rule_version,supersedes_roster_id)
        VALUES(?,?,?,?,?,?,?,?,?,?)''', (old['year'], old['tier'], version, _json(requirements), utcnow_iso(), utcnow_iso(),
                                        str(actor), reason, badges.RULE_VERSION, old['id'])).lastrowid


def _preview(conn, roster_id, requirements):
    old, requirements, warnings = _validate(conn, roster_id, requirements)
    fingerprint = _fingerprint(conn)
    members = _members(conn, old['year'], old['tier'])
    conn.execute('SAVEPOINT roster_preview')
    try:
        before = _state(conn, members, old['year'], old['tier'])
        replacement = _append(conn, old, requirements, 'preview', 'Preview historical roster repair')
        for member in members:
            scoring.sync_user(conn, member)
            badges.sync_user(conn, member, operation_id='preview:roster-repair', actor='preview', reason='Preview historical roster repair', daily_scope=(old['year'], old['tier']), include_lifetime=False)
        after = _state(conn, members, old['year'], old['tier'])
    finally:
        conn.execute('ROLLBACK TO roster_preview')
        conn.execute('RELEASE roster_preview')
    impact = {member: {'before': before[member], 'after': after[member],
                       'points_delta': after[member]['points'] - before[member]['points']}
              for member in members if before[member] != after[member]}
    preview = {'version': 1, 'roster_id': roster_id, 'year': old['year'], 'tier': old['tier'],
               'original_requirements': json.loads(old['requirements_json']), 'requirements': requirements,
               'replacement_id': replacement, 'members': members, 'impact': impact, 'warnings': warnings,
               'state_version': fingerprint, 'scope': 'all affected members; selected roster/year/tier only'}
    preview['preview_version'] = _digest(preview)
    return preview


def preview_repair(roster_id, requirements):
    """No committed writes; missing evidence is never reconstructed as an unlock date."""
    with db_session() as conn:
        conn.execute('BEGIN')
        return _preview(conn, roster_id, requirements)


def apply_repair(preview, *, actor, reason, operation_id):
    _reason(actor, reason, operation_id)
    with db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        prior = _operation(conn, operation_id, 'roster_repair', preview, actor, reason)
        if prior is not None:
            return prior
        current = _preview(conn, preview['roster_id'], preview['requirements'])
        if current != preview:
            raise ValueError('Roster repair preview is stale or modified; preview again')
        old = dict(conn.execute('SELECT * FROM daily_rosters WHERE id=?', (preview['roster_id'],)).fetchone())
        replacement = _append(conn, old, preview['requirements'], actor, reason)
        for member in preview['members']:
            scoring.sync_user(conn, member)
            badges.sync_user(conn, member, operation_id=operation_id, actor=str(actor), reason=reason, daily_scope=(old['year'], old['tier']), include_lifetime=False)
        result = {'operation_id': operation_id, 'roster_id': old['id'], 'replacement_id': replacement,
                  'affected_members': len(preview['impact']),
                  'point_delta': sum(p['points_delta'] for p in preview['impact'].values())}
        # Ledger, receipts, supersession, source requirements and audit operation
        # commit together; an interrupted operation leaves none of them visible.
        _record_operation(conn, operation_id, 'roster_repair', preview, result, actor, reason)
        conn.execute("INSERT INTO meta(key,value) VALUES('scoring_generation','1') ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)")
        return result
