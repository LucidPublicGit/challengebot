from datetime import datetime, timedelta, timezone
import json

from app import catalog
from app.db import db_session
from app.services import attempts, badges
from tests.test_web_lifecycle import client, _apply
from tests.test_roster_repairs import complete


def _roster():
    with db_session() as conn:
        row = dict(conn.execute("SELECT * FROM daily_rosters WHERE year=2026 AND tier='easy' ORDER BY version DESC LIMIT 1").fetchone())
    return row, json.loads(row['requirements_json'])


def test_web_roster_preview_apply_history_and_idempotency(client):
    old, required = _roster()
    complete('web-roster', required[:-1])
    before = attempts.challenge_points('web-roster')
    page = client.get('/lifecycle/rosters')
    assert page.status_code == 200
    assert 'Historical Daily roster repair' in page.text
    data = {'roster_id': old['id'], 'requirements': '\n'.join(required[:-1]),
            'historical_repair': 'yes', 'reason': 'Explicit historical correction'}
    response = client.post('/lifecycle/rosters/preview', data=data)
    assert response.status_code == 200, response.text
    assert 'points_delta' in response.text and 'missing' in response.text
    assert attempts.challenge_points('web-roster') == before
    result = _apply(client, response, requirements='', roster_id='999999')
    assert result.status_code == 200, result.text
    assert _apply(client, response).text == result.text
    assert attempts.challenge_points('web-roster') == before + 2
    assert badges.duration_badges('web-roster', year=2026)[0]['glyph'] == '日'
    with db_session() as conn:
        assert json.loads(conn.execute('SELECT requirements_json FROM daily_rosters WHERE id=?', (old['id'],)).fetchone()[0]) == required
        operation = conn.execute("SELECT * FROM admin_operations WHERE operation_type='roster_repair'").fetchone()
        assert operation['actor'] == 'web:admin' and operation['reason'] == data['reason']
    assert 'roster_repair' in client.get('/lifecycle/history').text


def test_web_roster_requires_reason_acknowledgement_valid_scope_and_fresh_preview(client):
    old, required = _roster()
    data = {'roster_id': old['id'], 'requirements': '\n'.join(required[:-1]),
            'historical_repair': 'yes', 'reason': 'Fix wrong requirement'}
    for patch in ({'reason': ''}, {'historical_repair': ''}, {'roster_id': 'bad'},
                  {'requirements': 'tales-trails-2026'}, {'requirements': 'unknown'}):
        assert client.post('/lifecycle/rosters/preview', data={**data, **patch}).status_code == 400
    response = client.post('/lifecycle/rosters/preview', data=data)
    with db_session() as conn:
        conn.execute("UPDATE challenge_defs SET title='Concurrent edit' WHERE id='easy-1'")
    assert _apply(client, response).status_code == 409


def test_web_can_schedule_and_explicitly_cancel_restoration(client):
    retired = client.post('/lifecycle/challenges/easy-1/preview', data={
        'action': 'retire', 'identity': '0', 'reason': 'Temporary edition retirement'})
    assert _apply(client, retired).status_code == 200
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    restore = client.post('/lifecycle/challenges/easy-1/preview', data={
        'action': 'restore', 'identity': '0', 'reason': 'Restore at next availability window', 'effective_at': future})
    assert restore.status_code == 200, restore.text
    assert _apply(client, restore).status_code == 200
    assert catalog.get_challenge('easy-1')['restore_at'] == future
    assert catalog.get_challenge('easy-1')['lifecycle_state'] == 'retired'
    cancel = client.post('/lifecycle/challenges/easy-1/preview', data={
        'action': 'cancel_restore', 'identity': '0', 'reason': 'Cancel future reopening'})
    assert _apply(client, cancel).status_code == 200
    assert catalog.get_challenge('easy-1')['restore_at'] is None
    assert catalog.get_challenge('easy-1')['lifecycle_state'] == 'retired'
    assert _apply(client, cancel).status_code == 200
