import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app import catalog, db
from app.services import badges, catalog_import, lifecycle
from tests.test_catalog_admin import payload
from tests.test_roster_repairs import fixture, complete, _rows
from tests.test_web_lifecycle import client, _token


def apply(preview):
    return catalog_import.apply_import(preview, actor='import-admin', reason='Reviewed merge', operation_id='reviewed-import')


def test_atomic_merge_publishes_only_final_roster_preserves_awards_and_history(fixture):
    path, old, required = fixture
    complete('earned', required)
    complete('partial', required[:-1])
    data = {'year': 2026, 'challenges': [
        {**payload(required[-1]), 'duration_group': 'monthly'},
        {**payload(required[0]), 'id': 'imported-daily', 'identity_id': 'imported-daily',
         'edition_id': 'imported-daily', 'number': 999},
    ]}
    tables = ('challenge_defs', 'definition_revisions', 'daily_rosters', 'badge_receipts', 'award_ledger', 'attempt_events', 'admin_operations')
    before = {t: _rows(t) for t in tables}
    preview = catalog_import.preview_import(data)
    assert {t: _rows(t) for t in tables} == before
    assert preview['roster_impact']['2026:easy']['partial']['points_delta'] == 0
    preview = json.loads(json.dumps(preview))
    with patch.object(catalog_import, '_record_operation', side_effect=RuntimeError('Interrupted operation')):
        with pytest.raises(RuntimeError):
            apply(preview)
    assert {t: _rows(t) for t in tables} == before
    result = apply(preview)
    assert apply(preview) == result
    assert badges.duration_badges('partial', year=2026) == []
    assert badges.duration_badges('earned', year=2026)[0]['glyph'] == '日'
    with db.db_session() as conn:
        rosters = conn.execute("SELECT * FROM daily_rosters WHERE year=2026 AND tier='easy' ORDER BY version").fetchall()
        assert len(rosters) == 2  # No intermediate shrink roster that could falsely qualify later.
        assert set(json.loads(rosters[-1]['requirements_json'])) == set(required[:-1]) | {'imported-daily'}
        revisions = conn.execute("SELECT * FROM definition_revisions WHERE operation_id='reviewed-import'").fetchall()
        assert len(revisions) == 2
        assert all(r['actor'] == 'import-admin' and r['source'] == 'catalog_import' and r['reason'] == 'Reviewed merge' for r in revisions)
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    db.init_db(path)
    assert catalog.get_challenge(required[-1])['duration_group'] == 'monthly'
    assert catalog.get_challenge('imported-daily')


def test_merge_stale_tampered_and_overrides_lifecycle_changes_rejected(fixture):
    _, _, required = fixture
    cid = required[0]
    preview = catalog_import.preview_import({'year': 2026, 'challenges': [{'id': cid, 'title': 'Reviewed title'}]})
    tampered = copy.deepcopy(preview)
    tampered['after'][0]['title'] = 'Not reviewed'
    with pytest.raises(ValueError, match='stale or modified'):
        apply(tampered)
    lifecycle.retire_challenge(cid, actor='test', reason='Retire before import applies', operation_id='retirement')
    with pytest.raises(ValueError, match='stale or modified'):
        apply(preview)
    with pytest.raises(ValueError, match='lifecycle preview'):
        catalog_import.preview_import({'year': 2026, 'challenges': [{'id': cid, 'lifecycle_state': 'active'}]})
    unchanged = catalog_import.preview_import({'year': 2026, 'challenges': [{'id': cid, 'title': 'Retired title correction'}]})
    apply(unchanged)
    assert catalog.get_challenge(cid)['lifecycle_state'] == 'retired'
    with db.db_session() as conn:
        conn.execute('UPDATE challenge_defs SET points=7 WHERE id=?', (cid,))
    omitted = catalog_import.preview_import({'year': 2026, 'challenges': [{'id': cid, 'description': 'Keep explicit override'}]})
    assert omitted['data']['challenges'][0]['points'] == 7
    for points in (None, 3):
        with pytest.raises(ValueError, match='scoring preview'):
            catalog_import.preview_import({'year': 2026, 'challenges': [{'id': cid, 'points': points}]})


def test_yaml_validation_and_existing_catalog_source(fixture):
    source = Path(__file__).resolve().parents[1] / 'challenges' / '2026.yaml'
    data = catalog_import.parse_yaml(source.read_bytes())
    preview = catalog_import.preview_import(data)
    assert len(preview['data']['challenges']) == len(data['challenges'])
    for data in ({'year': True, 'challenges': [{}]}, {'year': 2026, 'challenges': []},
                 {'year': 2026, 'challenges': [{'id': 'easy-1'}, {'id': 'easy-1'}]},
                 {'year': 2026, 'challenges': [{'id': 'easy-1', 'title': []}]}):
        with pytest.raises(ValueError):
            catalog_import.preview_import(data)
    with pytest.raises(ValueError, match='Invalid catalog YAML'):
        catalog_import.parse_yaml('!!python/object/apply:os.system ["not-executed"]')
    with pytest.raises(ValueError, match='1 MiB'):
        catalog_import.parse_yaml('x' * (catalog_import.MAX_BYTES + 1))


def test_web_import_requires_auth_reason_and_review_before_writes(client):
    body = yaml.safe_dump({'year': 2026, 'challenges': [{'id': 'easy-1', 'title': 'Private reviewed merge'}]})
    assert client.post('/challenges/seed', data={'catalog_yaml': body, 'reason': 'Review'}, auth=('admin', 'wrong')).status_code == 401
    assert client.post('/challenges/seed', data={'catalog_yaml': body}).status_code == 400
    response = client.post('/challenges/seed', data={'catalog_yaml': body, 'reason': 'Human review'})
    token = _token(response)
    assert catalog.get_challenge('easy-1')['title'] != 'Private reviewed merge'
    # A submitted replacement payload cannot change the captured operation body.
    result = client.post('/lifecycle/apply', data={'token': token, 'confirmed': 'yes', 'catalog_yaml': 'ignored'})
    assert result.status_code == 200, result.text
    assert catalog.get_challenge('easy-1')['title'] == 'Private reviewed merge'
    assert client.post('/lifecycle/apply', data={'token': token, 'confirmed': 'yes'}).status_code == 200


def test_web_catalog_path_guard_rejects_external_file_before_open(client):
    with patch.object(Path, 'open', side_effect=AssertionError('No file may be opened')):
        response = client.post('/challenges/seed', data={'path': '/outside/never-open.yaml', 'reason': 'Review source'})
    assert response.status_code == 400
