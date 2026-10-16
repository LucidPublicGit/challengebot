import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import bot_commands, catalog, db
from app.services import admin_completion, attempts, badges, lifecycle
from tests.helpers import setup_tmp_db, cleanup_db


@pytest.fixture
def fixture():
    path = setup_tmp_db()
    try:
        yield path
    finally:
        cleanup_db(path)


def attempt(member='synthetic', *, status='active', expired=False):
    attempts.ensure_user(member)
    ch = catalog.get_challenge('tales-trails-2026')
    start = datetime.now(timezone.utc) - timedelta(days=60 if expired else 1)
    with db.db_session() as conn:
        aid = conn.execute('INSERT INTO attempts(discord_id,challenge_id,param,status,started_at,ends_at,evidence_json,note) VALUES(?,?,?,?,?,?,?,?)',
                           (member, ch['id'], 'Synthetic selected game', status, start.isoformat(), lifecycle.calculate_deadline(ch, start).isoformat(),
                            json.dumps({'prior_reference': 'synthetic-evidence'}), 'Prior note')).lastrowid
        lifecycle.capture_attempt(conn, aid, ch)
    return attempts.get_attempt(aid)


def snapshot():
    with db.db_session() as conn:
        return {table: [dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('attempts', 'attempt_events', 'award_ledger', 'badge_receipts', 'admin_operations', 'daily_rosters')}


def test_diagnostics_are_read_only_even_with_overdue_and_due_scheduled_attempts(fixture):
    attempt()
    attempt('expired', expired=True)
    attempt('due', status='scheduled')
    original = snapshot()
    for member in ('synthetic', 'expired', 'due'):
        for mode in ('fake_pass', 'fake_fail', 'force'):
            asyncio.run(attempts.admin_test_verify(member, mode=mode, apply=False))
            assert snapshot() == original
            assert not asyncio.run(attempts.admin_test_verify(member, mode=mode, apply=True))['ok']
            assert snapshot() == original
    assert not asyncio.run(attempts.confirm_attempt('synthetic', force_honor=True))['ok']
    assert not asyncio.run(attempts.confirm_attempt('synthetic', fake_logs=[]))['ok']
    assert snapshot() == original


def test_attestation_preserves_evidence_and_snapshots_and_is_reviewed_audited_idempotent(fixture):
    att = attempt()
    original = snapshot()
    actual = datetime.now(timezone.utc) - timedelta(hours=1)
    preview = admin_completion.preview_attestation(att['id'], discord_id='synthetic', actual_completed_at=actual,
                                                 actor='discord:42', reason='Verified real completion', content='Synthetic selected game')
    assert snapshot() == original
    result = lifecycle.apply_correction(preview, actor='discord:42', reason='Verified real completion', operation_id='attested')
    assert lifecycle.apply_correction(preview, actor='discord:42', reason='Retry', operation_id='attested') == result
    after = attempts.get_attempt(att['id'])
    assert after['rules_snapshot_json'] == att['rules_snapshot_json']
    assert after['scoring_snapshot_json'] == att['scoring_snapshot_json']
    assert after['actual_completed_at'] == actual.isoformat()
    evidence = json.loads(after['evidence_json'])
    assert evidence['prior_reference'] == 'synthetic-evidence'
    assert evidence['administrative_attestation']['synthetic_verification'] is False
    assert evidence['administrative_attestation']['actor'] == 'discord:42'
    assert after['note'].startswith('Prior note\n')
    assert attempts.challenge_points('synthetic') == 10
    assert badges.duration_badges('synthetic')[0]['glyph'] == '月'
    with db.db_session() as conn:
        event = conn.execute("SELECT * FROM attempt_events WHERE operation_id='attested'").fetchone()
        assert event['actor'] == 'discord:42' and event['reason'] == 'Verified real completion'
        receipt = conn.execute('SELECT * FROM award_ledger WHERE attempt_id=?', (att['id'],)).fetchone()
        assert json.loads(receipt['metadata_json'])['administrative_exception'] is True
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_attestation_validation_and_stale_preview(fixture):
    att = attempt()
    args = dict(discord_id='synthetic', actual_completed_at=datetime.now(timezone.utc), actor='reviewer', reason='Review')
    for changes in ({'discord_id': 'different-member'}, {'reason': ''},
                    {'actual_completed_at': datetime.now(timezone.utc) + timedelta(days=1)},
                    {'actual_completed_at': '2026-01-01T00:00:00'}):
        with pytest.raises(ValueError):
            admin_completion.preview_attestation(att['id'], **{**args, **changes})
    p = admin_completion.preview_attestation(att['id'], **args)
    attempts.user_update_attempt_content('synthetic', attempt_id=att['id'], param='Audited spelling correction')
    with pytest.raises(ValueError, match='stale'):
        lifecycle.apply_correction(p, actor='reviewer', reason='Review', operation_id='stale-attestation')
    assert attempts.challenge_points('synthetic') == 0
    old = attempt('expired-member', status='expired', expired=True)
    with pytest.raises(ValueError, match='exception_reason'):
        admin_completion.preview_attestation(old['id'], **{**args, 'discord_id': 'expired-member'})
    late = admin_completion.preview_attestation(old['id'], **{**args, 'discord_id': 'expired-member', 'exception_reason': 'Deliberately accept a late exceptional clear'})
    assert late['warnings']


def test_attestation_modal_denies_wrong_owner_or_removed_role_before_preview():
    async def check():
        modal = bot_commands.AdminCompletionModal(42, target_id='synthetic')
        for user, admin in ((43, True), (42, False)):
            ctx = SimpleNamespace(user=SimpleNamespace(id=user), response=AsyncMock())
            with patch.object(bot_commands, '_admin_only', return_value=admin), patch.object(admin_completion, 'preview_attestation') as preview:
                await modal.on_submit(ctx)
                preview.assert_not_called()
                ctx.response.send_message.assert_awaited_once()
    asyncio.run(check())
