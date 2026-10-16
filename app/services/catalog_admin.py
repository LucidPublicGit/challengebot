"""Reviewed catalog definitions and prospective Daily roster impact.

No live/network concerns are hidden here: every helper takes the caller's SQLite
transaction, and preview rolls back the actual writer and dependency projections.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone

from app.db import db_session, utcnow_iso
from app.services import attempts, badges, rosters, scoring, lifecycle
from app.services.lifecycle import _date, _fingerprint, _operation, _reason, _record_operation


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _validate(conn, data):
    if not isinstance(data, dict):
        raise ValueError('Catalog definition must be an object')
    allowed = {'id', 'identity_id', 'edition_id', 'year', 'tier', 'number', 'title', 'description',
               'time_limit_hours', 'param_label', 'verify', 'goal_kind', 'goal_amount', 'media_filter',
               'allows_concurrent', 'fixed_param', 'points', 'category', 'duration_group', 'repeat_policy',
               'available_from', 'available_until', 'published_at', 'actor', 'reason', 'source'}
    if data.keys() - allowed:
        raise ValueError('Unsupported catalog definition fields')
    result = dict(data)
    if not isinstance(result.get('id'), str) or not result['id'].strip() or len(result['id']) > 200:
        raise ValueError('A nonempty edition ID of at most 200 characters is required')
    existing = conn.execute('SELECT * FROM challenge_defs WHERE id=?', (result['id'],)).fetchone()
    if not isinstance(result.get('title'), str) or not result['title'].strip():
        raise ValueError('A nonempty text title is required')
    for key in ('description', 'param_label', 'fixed_param', 'media_filter', 'verify', 'goal_kind', 'identity_id', 'edition_id'):
        if key in result and result[key] is not None and not isinstance(result[key], str):
            raise ValueError(f'{key} must be text or null')
    if result.get('verify', 'tadoku') not in ('tadoku', 'honor'):
        raise ValueError('Invalid verification policy')
    if 'allows_concurrent' in result and result['allows_concurrent'] not in (False, True, 0, 1, 'false', 'true', '0', '1'):
        raise ValueError('Concurrency must be a boolean')
    for key in ('year', 'number'):
        if isinstance(result.get(key), bool) or not isinstance(result.get(key), int) or not 0 < result[key] <= 2**31-1:
            raise ValueError(f'{key} must be a positive integer')
    if result.get('tier') not in ('easy', 'medium', 'hard', 'ota_master', 'special'):
        raise ValueError('Invalid difficulty tier')
    for key in ('goal_amount', 'time_limit_hours'):
        try:
            value = float(result.get(key, 24 if key == 'time_limit_hours' else None))
            if not math.isfinite(value) or value < 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f'{key} must be a finite nonnegative number') from exc
        result[key] = value
    raw = result.get('points')
    if raw in (None, ''):
        result['points'] = None
    else:
        try:
            if isinstance(raw, bool) or str(int(raw)) != str(raw) or int(raw) < 0 or int(raw) > 2**31-1:
                raise ValueError
            result['points'] = int(raw)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError('Points override must be a nonnegative integer') from exc
    if existing and result['points'] != existing['points']:
        raise ValueError('Use the scoring preview to change or explicitly clear an existing points override')
    result.setdefault('duration_group', existing['duration_group'] if existing else ('special' if result['tier'] == 'special' else 'daily'))
    result.setdefault('repeat_policy', existing['repeat_policy'] if existing else 'once_ever')
    for key in ('published_at', 'available_from', 'available_until'):
        if key in result:
            result[key] = _date(result[key]).isoformat() if result[key] else None
    start = result.get('available_from', existing['available_from'] if existing else None)
    end = result.get('available_until', existing['available_until'] if existing else None)
    if start and end and _date(end) <= _date(start):
        raise ValueError('Availability end must follow its start')
    return result, dict(existing) if existing else None


def _cohorts(before, data):
    result = set()
    for definition in (before, data):
        if definition and definition.get('duration_group', 'daily') == 'daily' and definition.get('tier') in badges.TIERS:
            result.add((int(definition['year']), definition['tier']))
    return [list(pair) for pair in sorted(result)]


def _progress(conn, cohorts):
    return {f'{year}:{tier}': rosters._state(conn, rosters._members(conn, year, tier), year, tier)
            for year, tier in cohorts}


def _sync(conn, cohorts, *, actor, reason, operation_id):
    for year, tier in cohorts:
        for member in rosters._members(conn, year, tier):
            scoring.sync_user(conn, member, actor=actor, reason=reason, operation_id=operation_id)
            badges.sync_user(conn, member, operation_id=operation_id, actor=actor, reason=reason,
                             daily_scope=(year, tier), include_lifetime=False)


def _write(conn, data, *, publish=True):
    try:
        result = attempts.admin_upsert_challenge(data, conn=conn, publish=publish)
    except sqlite3.IntegrityError as exc:
        raise ValueError('Catalog revision conflicts with an existing edition, number, or reference') from exc
    if not result.get('ok'):
        raise ValueError(result.get('error') or 'Catalog revision rejected')


def _preview(conn, data):
    data, before = _validate(conn, data)
    cohorts = _cohorts(before, data)
    version = _fingerprint(conn)
    conn.execute('SAVEPOINT catalog_preview')
    try:
        original = _progress(conn, cohorts)
        _write(conn, {**data, 'actor': 'preview', 'reason': 'Preview catalog revision', 'source': 'preview'})
        _sync(conn, cohorts, actor='preview', reason='Preview prospective roster effects', operation_id='preview:catalog')
        after = dict(conn.execute('SELECT * FROM challenge_defs WHERE id=?', (data['id'],)).fetchone())
        effective = _progress(conn, cohorts)
    finally:
        conn.execute('ROLLBACK TO catalog_preview')
        conn.execute('RELEASE catalog_preview')
    impact = {}
    for cohort in original:
        for member in original[cohort].keys() | effective[cohort].keys():
            old = original[cohort].get(member)
            new = effective[cohort].get(member)
            if old != new:
                impact.setdefault(cohort, {})[member] = {'before': old, 'after': new,
                    'points_delta': (new['points'] if new else 0) - (old['points'] if old else 0)}
    preview = {'version': 1, 'data': data, 'before': before, 'after': after, 'cohorts': cohorts,
               'roster_impact': impact, 'state_version': version,
               'policy': 'Prospective roster versions only; already-earned Daily receipts and bonuses are preserved'}
    preview['preview_version'] = hashlib.sha256(_json(preview).encode()).hexdigest()
    return preview


def preview_definition(data):
    with db_session() as conn:
        conn.execute('BEGIN')
        return _preview(conn, data)


def apply_definition(preview, *, actor, reason, operation_id):
    _reason(actor, reason, operation_id)
    with db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        prior = _operation(conn, operation_id, 'catalog_revision', preview, actor, reason)
        if prior is not None:
            return prior
        current = _preview(conn, preview['data'])
        if current != preview:
            raise ValueError('Catalog revision preview is stale or modified; preview again')
        before_revision = conn.execute('SELECT COALESCE(MAX(id),0) FROM definition_revisions').fetchone()[0]
        _write(conn, {**preview['data'], 'actor': str(actor), 'reason': reason, 'source': 'admin'})
        conn.execute('UPDATE definition_revisions SET operation_id=? WHERE id>?', (operation_id, before_revision))
        _sync(conn, preview['cohorts'], actor=str(actor), reason=reason, operation_id=operation_id)
        result = {'operation_id': operation_id, 'challenge_id': preview['data']['id'],
                  'roster_impact': preview['roster_impact']}
        _record_operation(conn, operation_id, 'catalog_revision', preview, result, actor, reason)
        return result


def _clean_record(record):
    return {k: v for k, v in record.items() if k != 'updated_at'}


def _preview_transition(conn, data):
    if data.get('action') not in ('retire', 'restore') or not isinstance(data.get('identity'), bool):
        raise ValueError('Choose retirement/restoration and identity/edition scope')
    row = conn.execute('SELECT * FROM challenge_defs WHERE id=?', (data.get('challenge_id'),)).fetchone()
    if row is None:
        raise ValueError('Unknown challenge')
    ch = dict(row)
    related = [dict(r) for r in conn.execute('SELECT * FROM challenge_defs WHERE identity_id=?', (ch['identity_id'],))] if data['identity'] else [ch]
    cohorts = sorted({(int(c['year']), c['tier']) for c in related if c['duration_group'] == 'daily' and c['tier'] in badges.TIERS})
    cohorts = [list(pair) for pair in cohorts]
    version = _fingerprint(conn)
    conn.execute('SAVEPOINT lifecycle_catalog_preview')
    try:
        original = _progress(conn, cohorts)
        result = lifecycle._lifecycle(data['challenge_id'], actor='preview', reason=data['reason'], identity=data['identity'],
                                      effective_at=data['effective_at'], operation_id='preview:lifecycle-catalog',
                                      restore=data['action'] == 'restore', conn=conn, record_operation=False)
        _sync(conn, cohorts, actor='preview', reason=data['reason'], operation_id='preview:lifecycle-catalog')
        effective = _progress(conn, cohorts)
        forecast = None
        if _date(data['effective_at']) > datetime.now(timezone.utc):
            lifecycle.materialize_restorations(conn, at=data['effective_at'])
            badges.publish_rosters(conn, at=_date(data['effective_at']))
            _sync(conn, cohorts, actor='preview', reason=data['reason'], operation_id='preview:lifecycle-forecast')
            forecast = _progress(conn, cohorts)
    finally:
        conn.execute('ROLLBACK TO lifecycle_catalog_preview')
        conn.execute('RELEASE lifecycle_catalog_preview')
    preview = {'version': 1, 'data': data, 'before': _clean_record(result['before']),
               'after': _clean_record(result['after']), 'cohorts': cohorts,
               'progress_before': original, 'progress_after_now': effective,
               'progress_at_effective_time_forecast': forecast,
               'forecast_policy': 'Uses only currently known completions; no future clears or unlock dates are invented',
               'state_version': version}
    preview['preview_version'] = hashlib.sha256(_json(preview).encode()).hexdigest()
    return preview


def preview_lifecycle(challenge_id, *, action, identity=False, effective_at=None, reason):
    if not str(reason or '').strip():
        raise ValueError('Audit reason required')
    data = {'challenge_id': challenge_id, 'action': action, 'identity': identity,
            'effective_at': _date(effective_at).isoformat() if effective_at else utcnow_iso(), 'reason': reason}
    with db_session() as conn:
        conn.execute('BEGIN')
        return _preview_transition(conn, data)


def apply_lifecycle(preview, *, actor, reason, operation_id):
    _reason(actor, reason, operation_id)
    with db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        prior = _operation(conn, operation_id, 'catalog_lifecycle', preview, actor, reason)
        if prior is not None:
            return prior
        current = _preview_transition(conn, preview['data'])
        if current != preview:
            raise ValueError('Lifecycle preview is stale or modified; preview again')
        data = preview['data']
        result = lifecycle._lifecycle(data['challenge_id'], actor=str(actor), reason=reason, identity=data['identity'],
                                      effective_at=data['effective_at'], operation_id=operation_id,
                                      restore=data['action'] == 'restore', conn=conn, record_operation=False)
        _sync(conn, preview['cohorts'], actor=str(actor), reason=reason, operation_id=operation_id)
        result['operation_id'] = operation_id
        _record_operation(conn, operation_id, 'catalog_lifecycle', preview, result, actor, reason)
        return result
