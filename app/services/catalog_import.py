"""Reviewed, atomic catalog merges. No filesystem reads or implicit retirement.

The transport supplies parsed YAML; apply uses the exact captured payload, never
reopens a mutable source file. Startup's seed-only importer remains separate.
"""
from __future__ import annotations

import hashlib

import yaml

from app.db import db_session
from app.services import badges, catalog_admin as admin
from app.services.lifecycle import _fingerprint, _operation, _reason, _record_operation

MAX_BYTES = 1024 * 1024
MAX_ENTRIES = 1000
FIELDS = {'id', 'identity_id', 'edition_id', 'year', 'tier', 'number', 'title', 'description',
          'time_limit_hours', 'param_label', 'verify', 'goal_kind', 'goal_amount', 'media_filter',
          'allows_concurrent', 'fixed_param', 'points', 'category', 'duration_group', 'repeat_policy',
          'available_from', 'available_until', 'published_at'}
LIFECYCLE_FIELDS = {'lifecycle_state', 'retired_at', 'restore_at'}


def parse_yaml(content):
    if not isinstance(content, (str, bytes)):
        raise ValueError('Supply catalog YAML text')
    if len(content.encode('utf-8') if isinstance(content, str) else content) > MAX_BYTES:
        raise ValueError('Catalog YAML exceeds the 1 MiB limit')
    try:
        return yaml.safe_load(content)
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ValueError('Invalid catalog YAML') from exc


def _normalize(conn, catalog):
    if not isinstance(catalog, dict) or catalog.keys() - {'year', 'challenges'}:
        raise ValueError('Catalog must contain year and challenges')
    year, items = catalog.get('year'), catalog.get('challenges')
    if isinstance(year, bool) or not isinstance(year, int) or not 0 < year <= 2**31 - 1:
        raise ValueError('Catalog year must be a positive integer')
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ENTRIES:
        raise ValueError('Catalog must contain between 1 and 1000 definitions')
    normalized, seen = [], set()
    for raw in items:
        if not isinstance(raw, dict) or raw.keys() - FIELDS - LIFECYCLE_FIELDS:
            raise ValueError('Unsupported catalog definition fields')
        cid = raw.get('id')
        if not isinstance(cid, str) or not cid.strip() or cid in seen:
            raise ValueError('Every definition needs a unique nonempty ID')
        seen.add(cid)
        if raw.get('year', year) != year:
            raise ValueError('Definition year must match the catalog year')
        row = conn.execute('SELECT * FROM challenge_defs WHERE id=?', (cid,)).fetchone()
        existing = dict(row) if row else None
        # A merge is not a lifecycle action, including accidental resurrection.
        for key in LIFECYCLE_FIELDS & raw.keys():
            expected = existing.get(key) if existing else ('active' if key == 'lifecycle_state' else None)
            if raw[key] != expected:
                raise ValueError('Use the lifecycle preview to retire, restore or reschedule an edition')
        data = {k: v for k, v in (existing or {}).items() if k in FIELDS}
        data.update({k: v for k, v in raw.items() if k in FIELDS})
        data['year'] = year
        data, before = admin._validate(conn, data)
        normalized.append((data, before))
    return {'year': year, 'challenges': [data for data, _ in normalized]}, normalized


def _write_all(conn, data, *, actor, reason):
    for item in data['challenges']:
        admin._write(conn, {**item, 'actor': str(actor), 'source': 'catalog_import', 'reason': reason}, publish=False)
    # Only the final, complete catalog is a prospective roster. Intermediate
    # definition states must never become alternative qualifying historical sets.
    badges.publish_rosters(conn)


def _preview(conn, catalog):
    data, rows = _normalize(conn, catalog)
    cohorts = sorted({tuple(pair) for item, before in rows for pair in admin._cohorts(before, item)})
    cohorts = [list(pair) for pair in cohorts]
    version = _fingerprint(conn)
    conn.execute('SAVEPOINT catalog_merge_preview')
    try:
        original = admin._progress(conn, cohorts)
        _write_all(conn, data, actor='preview', reason='Preview catalog merge')
        admin._sync(conn, cohorts, actor='preview', reason='Preview catalog merge', operation_id='preview:catalog-import')
        after = [dict(conn.execute('SELECT * FROM challenge_defs WHERE id=?', (item['id'],)).fetchone()) for item in data['challenges']]
        effective = admin._progress(conn, cohorts)
    finally:
        conn.execute('ROLLBACK TO catalog_merge_preview')
        conn.execute('RELEASE catalog_merge_preview')
    impact = {}
    for cohort in original:
        for member in original[cohort].keys() | effective[cohort].keys():
            old, new = original[cohort].get(member), effective[cohort].get(member)
            if old != new:
                impact.setdefault(cohort, {})[member] = {'before': old, 'after': new,
                    'points_delta': (new['points'] if new else 0) - (old['points'] if old else 0)}
    preview = {'version': 1, 'data': data, 'before': [before for _, before in rows], 'after': after,
               'cohorts': cohorts, 'roster_impact': impact, 'state_version': version,
               'policy': 'Merge only; omissions and existing histories are preserved. No implicit override or lifecycle changes. Only final roster sets are published.'}
    preview['preview_version'] = hashlib.sha256(admin._json(preview).encode()).hexdigest()
    return preview


def preview_import(catalog):
    with db_session() as conn:
        conn.execute('BEGIN')
        return _preview(conn, catalog)


def apply_import(preview, *, actor, reason, operation_id):
    _reason(actor, reason, operation_id)
    with db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = _operation(conn, operation_id, 'catalog_import', preview, actor, reason)
        if previous is not None:
            return previous
        if _preview(conn, preview['data']) != preview:
            raise ValueError('Catalog import preview is stale or modified; preview again')
        first_revision = conn.execute('SELECT COALESCE(MAX(id),0) FROM definition_revisions').fetchone()[0]
        _write_all(conn, preview['data'], actor=actor, reason=reason)
        conn.execute('UPDATE definition_revisions SET operation_id=? WHERE id>?', (operation_id, first_revision))
        admin._sync(conn, preview['cohorts'], actor=str(actor), reason=reason, operation_id=operation_id)
        result = {'operation_id': operation_id, 'challenge_ids': [c['id'] for c in preview['data']['challenges']],
                  'roster_impact': preview['roster_impact']}
        _record_operation(conn, operation_id, 'catalog_import', preview, result, actor, reason)
        return result
