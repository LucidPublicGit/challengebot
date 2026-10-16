import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import db
from app import bot_commands as commands
from app.services import attempts, lifecycle
from tests.helpers import setup_tmp_db, cleanup_db
from tests.test_scoring_classification import _completion


def interaction(user=42):
    return SimpleNamespace(user=SimpleNamespace(id=user), response=AsyncMock(), original_response=AsyncMock())


def test_discord_review_rechecks_owner_role_and_stale_state():
    path = setup_tmp_db()
    try:
        aid, _ = _completion()
        preview = lifecycle.preview_correction(aid, {'status': 'void'}, exception_reason='Explicit administrative void')
        async def check():
            view = commands.AdminOperationReviewView(42, kind='correction', preview=preview, reason='Explicit administrative void')
            with patch.object(commands, '_admin_only', return_value=True):
                wrong = interaction(43)
                await view.apply_review.callback(wrong)
                assert attempts.challenge_points('synthetic') == 10
            with patch.object(commands, '_admin_only', return_value=False):
                denied = interaction()
                await view.apply_review.callback(denied)
                assert attempts.challenge_points('synthetic') == 10
            with db.db_session() as conn:
                conn.execute("UPDATE attempts SET note='Concurrent correction' WHERE id=?", (aid,))
            with patch.object(commands, '_admin_only', return_value=True):
                stale = interaction()
                await view.apply_review.callback(stale)
                assert 'stale' in stale.response.send_message.call_args.args[0].lower()
                assert attempts.challenge_points('synthetic') == 10
        asyncio.run(check())
    finally:
        cleanup_db(path)


def test_discord_review_audits_actor_reason_and_repeated_apply_once():
    path = setup_tmp_db()
    try:
        aid, _ = _completion()
        preview = lifecycle.preview_correction(aid, {'status': 'void'}, exception_reason='Explicit administrative void')
        async def check():
            view = commands.AdminOperationReviewView(42, kind='correction', preview=preview, reason='Correct a falsely verified clear')
            with patch.object(commands, '_admin_only', return_value=True):
                await view.apply_review.callback(interaction())
                await view.apply_review.callback(interaction())
            assert attempts.challenge_points('synthetic') == 0
            with db.db_session() as conn:
                operation = conn.execute('SELECT * FROM admin_operations WHERE operation_id=?', (view.operation_id,)).fetchone()
                assert operation['actor'] == 'discord:42'
                assert operation['reason'] == 'Correct a falsely verified clear'
                assert conn.execute('SELECT COUNT(*) FROM point_adjustments WHERE operation_id=?', (view.operation_id,)).fetchone()[0] == 1
        asyncio.run(check())
    finally:
        cleanup_db(path)


def test_discord_preview_is_private_complete_in_memory_attachment():
    async def check():
        ctx = interaction()
        preview = {'all_details': 'Synthetic text ' * 2000, 'affected_members': list(range(100))}
        await commands._present_admin_review(ctx, 'catalog', preview, 'Synthetic review')
        args = ctx.response.send_message.call_args.kwargs
        assert args['ephemeral'] is True
        assert args['file'].filename == 'admin-operation-preview.json'
        assert json.loads(args['file'].fp.read()) == preview
        args['file'].close()
    asyncio.run(check())
