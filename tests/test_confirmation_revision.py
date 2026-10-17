"""Provider waits must not overwrite corrections committed during verification."""
import asyncio
import json
from unittest.mock import patch

import pytest

from app import db
from app.services import attempts, lifecycle, scoring
from app.services.verify import VerifyResult
from tests.helpers import cleanup_db, setup_tmp_db


@pytest.mark.parametrize('matching', [{'aliases': 'Alternate title'}, {'log_ids': 'synthetic-log'}])
@pytest.mark.parametrize('interleave', ['before_save', 'after_save', None])
def test_matching_save_cannot_adopt_unverified_revision(matching, interleave):
    path = setup_tmp_db()
    try:
        cid = 'tales-trails-2026'
        with db.db_session() as conn:
            conn.execute("UPDATE challenge_defs SET verify='tadoku',goal_kind='hours',goal_amount=1 WHERE id=?", (cid,))
        attempts.link_tadoku('matching-member', 'synthetic-provider-id')
        started = attempts.start_attempt('matching-member', challenge_id=cid, param='Original game', guild_id='synthetic')
        assert started['ok'], started
        aid = started['attempt_id']
        save = attempts.user_update_attempt_content

        def correct():
            preview = lifecycle.preview_correction(aid, {'param': 'Corrected game'})
            lifecycle.apply_correction(preview, actor='reviewer', reason='Correct selected work', operation_id='matching-race')

        def interleaved_save(*args, **kwargs):
            if interleave == 'before_save':
                correct()
            result = save(*args, **kwargs)
            if interleave == 'after_save':
                correct()
            return result

        async def fetch(**kwargs):
            return []

        passing = VerifyResult(True, 1, 1, 'hours', 'Synthetic old-title-only pass')
        with patch.object(attempts, 'user_update_attempt_content', side_effect=interleaved_save), \
                patch.object(attempts, 'resolve_logs_for_user', side_effect=fetch) as provider, \
                patch.object(attempts, 'evaluate_logs', return_value=passing) as evaluate:
            result = asyncio.run(attempts.confirm_attempt('matching-member', attempt_id=aid, **matching))
        after = attempts.get_attempt(aid)
        if interleave:
            assert not result['ok'], result
            assert 'changed' in result['error']
            assert after['status'] == 'active'
            assert after['param'] == 'Corrected game'
            with db.db_session() as conn:
                assert not conn.execute('SELECT 1 FROM award_ledger WHERE source_key=?', (f'completion:{aid}',)).fetchone()
            if interleave == 'before_save':
                provider.assert_not_called()
                evaluate.assert_not_called()
                assert not after['fixup_json']
        else:
            assert result['ok'], result
            assert evaluate.call_args.kwargs['param'] == 'Original game'
            assert evaluate.call_args.kwargs['aliases'] == (['Alternate title'] if 'aliases' in matching else None)
            assert evaluate.call_args.kwargs['include_log_ids'] == (['synthetic-log'] if 'log_ids' in matching else None)
            assert after['status'] == 'completed'
            assert after['correction_revision'] == 1
            assert attempts.challenge_points('matching-member') == 10
    finally:
        cleanup_db(path)


@pytest.mark.parametrize('change', ['classification', 'owner_content', 'void', 'scoring'])
def test_confirmation_rechecks_state_after_provider_wait(change):
    path = setup_tmp_db()
    try:
        cid = 'tales-trails-2026'
        with db.db_session() as conn:
            conn.execute("UPDATE challenge_defs SET verify='tadoku',goal_kind='hours',goal_amount=1 WHERE id=?", (cid,))
        attempts.link_tadoku('race-member', 'synthetic-provider-id')
        started = attempts.start_attempt('race-member', challenge_id=cid, param='Selected game', guild_id='synthetic')
        assert started['ok'], started
        aid = started['attempt_id']
        captured = {}

        async def fetch(**kwargs):
            # Simulate a separate reviewed transaction committing while the
            # original confirmation is suspended waiting for provider evidence.
            if change == 'classification':
                preview = lifecycle.preview_correction(aid, {'tier': 'hard', 'duration_group': 'yearly'}, recompute_deadline=True)
                lifecycle.apply_correction(preview, actor='reviewer', reason='Correct accepted classification', operation_id='inflight-classification')
            elif change == 'owner_content':
                assert attempts.user_update_attempt_content('race-member', attempt_id=aid, param='Corrected game', reason='Correct title')['ok']
            elif change == 'void':
                preview = lifecycle.preview_correction(aid, {'status': 'void'}, exception_reason='Wrong attempt')
                lifecycle.apply_correction(preview, actor='reviewer', reason='Void wrong attempt', operation_id='inflight-void')
            else:
                preview = scoring.preview_recalculation({'multipliers': {'monthly': 5}}, {'edition_ids': [cid]}, include_open=True)
                scoring.apply_recalculation(preview, actor='reviewer', reason='Reviewed scoring change', operation_id='inflight-scoring')
            captured.update(attempts.get_attempt(aid))
            return []

        passing = VerifyResult(True, 1, 1, 'hours', 'Synthetic pass')
        with patch.object(attempts, 'resolve_logs_for_user', side_effect=fetch), patch.object(attempts, 'evaluate_logs', return_value=passing):
            result = asyncio.run(attempts.confirm_attempt('race-member', attempt_id=aid))
        after = attempts.get_attempt(aid)
        if change == 'scoring':
            assert result['ok'], result
            assert attempts.challenge_points('race-member') == 5
            assert after['scoring_snapshot_json'] == captured['scoring_snapshot_json']
            assert json.loads(after['effective_completion_json'])['scoring_snapshot_json'] == captured['scoring_snapshot_json']
        else:
            assert not result['ok'], result
            assert 'changed' in result['error'] or 'no longer active' in result['error']
            for key in ('status', 'correction_revision', 'effective_completion_json', 'rules_snapshot_json', 'param', 'evidence_json'):
                assert after[key] == captured[key]
            with db.db_session() as conn:
                assert not conn.execute('SELECT 1 FROM award_ledger WHERE source_key=?', (f'completion:{aid}',)).fetchone()
            if change == 'classification':
                # A fresh confirmation uses the corrected rules and accepted
                # linear policy; the failed stale request grants no award.
                assert attempts._complete(after, method='honor', evidence={'honor': True}, note=None)['ok']
                assert attempts.challenge_points('race-member') == 150
        with db.db_session() as conn:
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        cleanup_db(path)
