"""Weekly windows and prospective scoring rollout on disposable data only."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import catalog
from app.bot_ui import duration_heading, section_pages, window_hours
from app.db import db_session, init_db
from app.services import attempts, lifecycle, scoring
from tests.helpers import cleanup_db, setup_tmp_db


@pytest.mark.parametrize('start', ['2026-12-29T12:34:56+00:00', '2024-02-25T05:30:00-08:00'])
def test_weekly_is_seven_full_days_in_utc(start):
    instant = datetime.fromisoformat(start)
    assert lifecycle.calculate_deadline({'duration_group': 'weekly', 'time_limit_hours': 24}, instant) == instant.astimezone(timezone.utc) + timedelta(days=7)
    end = instant + timedelta(days=7)
    assert window_hours({'started_at': start, 'ends_at': end.isoformat(), 'time_limit_hours': 24}) == 168


def test_weekly_navigation_and_removed_history_button():
    rows = [{'id': key, 'duration_group': key} for key in ('yearly', 'monthly', 'weekly', 'daily')]
    pages = section_pages(rows, lambda row: row['id'])
    assert [row['id'] for page, _ in pages for row in page] == ['daily', 'weekly', 'monthly', 'yearly']
    assert '🟩 **WEEKLY** ━' in pages[0][1]
    assert '🟦 **DAILY** ━' in pages[0][1]
    assert len({duration_heading(row) for row in rows}) == 4
    async def check():
        from app.bot_commands import ListTierView
        view = ListTierView(1, year=2026)
        assert not any(getattr(child, 'label', None) == 'Past Challenges' for child in view.children)
        view.stop()
    asyncio.run(check())


def test_new_defaults_and_weekly_honor_completion_concurrency_and_restart():
    path = setup_tmp_db(legacy_scoring=False)
    try:
        assert scoring.MULTIPLIERS == {'daily': 1, 'weekly': 2, 'monthly': 3, 'yearly': 4, 'special': 1}
        expected = {'easy': [1, 2, 3, 4], 'medium': [2, 3, 5, 6],
                    'hard': [3, 4, 6, 8], 'ota_master': [4, 5, 8, 10]}
        for tier, awards in expected.items():
            for duration, points in zip(('daily', 'weekly', 'monthly', 'yearly'), awards):
                assert catalog.reward_points({'id': 'synthetic', 'tier': tier, 'duration_group': duration}) == points
        result = attempts.admin_upsert_challenge(dict(
            id='weekly-game', identity_id='weekly-game', year=2026, tier='easy', number=990,
            title='Weekly story', goal_kind='complete_work', goal_amount=1, verify='honor',
            duration_group='weekly', repeat_policy='annual', time_limit_hours=24,
        ))
        assert result['ok'], result
        with db_session() as conn:
            conn.execute("UPDATE challenge_defs SET verify='honor',goal_kind='complete_work' WHERE id='easy-1'")
        daily = attempts.start_attempt('weekly-member', challenge_id='easy-1', param='Synthetic manga', guild_id='synthetic')
        assert daily['ok'], daily
        weekly = attempts.start_attempt('weekly-member', challenge_id='weekly-game', param='Synthetic story', guild_id='synthetic')
        assert weekly['ok'], weekly
        with db_session() as conn:
            saved = dict(conn.execute('SELECT * FROM attempts WHERE id=?', (weekly['attempt_id'],)).fetchone())
        assert datetime.fromisoformat(saved['ends_at']) - datetime.fromisoformat(saved['started_at']) == timedelta(days=7)
        assert json.loads(saved['scoring_snapshot_json'])['points'] == 2
        init_db(path)
        assert attempts.get_attempt(weekly['attempt_id'])['ends_at'] == saved['ends_at']
        wrong = asyncio.run(attempts.confirm_attempt('weekly-member', attempt_id=weekly['attempt_id'], content='Another story'))
        assert not wrong['ok']
        done = asyncio.run(attempts.confirm_attempt('weekly-member', attempt_id=weekly['attempt_id']))
        assert done['ok'], done
        with db_session() as conn:
            assert conn.execute('SELECT effective_points FROM award_ledger WHERE source_key=?', (f"completion:{weekly['attempt_id']}",)).fetchone()[0] == 2
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert not attempts.start_attempt('weekly-member', challenge_id='weekly-game', param='Retry', guild_id='synthetic')['ok']
        # Scheduled attempts use their scheduled actual start across year-end.
        start = datetime(2030, 12, 29, 12, 34, tzinfo=timezone.utc)
        scheduled = attempts.start_attempt('scheduled-weekly', challenge_id='weekly-game', param='Scheduled story', guild_id='synthetic', starts_at=start)
        assert scheduled['ok'], scheduled
        assert datetime.fromisoformat(scheduled['ends_at']) == start + timedelta(days=7)
        init_db(path)
        assert attempts.get_attempt(scheduled['attempt_id'])['ends_at'] == scheduled['ends_at']
    finally:
        cleanup_db(path)


def test_default_cutover_updates_open_preserves_completed_and_later_admin_changes():
    path = setup_tmp_db()
    try:
        old = attempts.start_attempt('old-monthly', challenge_id='tales-trails-2026', param='Synthetic game', guild_id='synthetic')
        assert old['ok'], old
        completed = attempts.start_attempt('completed-monthly', challenge_id='tales-trails-2026', param='Completed game', guild_id='synthetic')
        assert completed['ok'], completed
        assert asyncio.run(attempts.confirm_attempt('completed-monthly', attempt_id=completed['attempt_id']))['ok']
        with db_session() as conn:
            # Simulate the prior deployed schema and policy before its first
            # startup with the new duration defaults.
            conn.execute("DELETE FROM meta WHERE key='duration-defaults-compressed-v1'")
            conn.execute("DELETE FROM scoring_policies WHERE operation_id='duration-defaults-compressed-v1'")
            conn.execute("DELETE FROM admin_operations WHERE operation_id='duration-defaults-compressed-v1'")
            conn.execute('UPDATE attempts SET scoring_snapshot_json=NULL WHERE id=?', (old['attempt_id'],))
        init_db(path)
        with db_session() as conn:
            saved = conn.execute('SELECT scoring_snapshot_json FROM attempts WHERE id=?', (old['attempt_id'],)).fetchone()[0]
            assert json.loads(saved)['points'] == 3
            assert json.loads(saved)['policy']['formula_version'] == scoring.FORMULA_VERSION
            assert scoring.current_policy(conn)['multipliers']['monthly'] == 3
            assert scoring.user_points(conn, 'completed-monthly') == 10
        done = asyncio.run(attempts.confirm_attempt('old-monthly', attempt_id=old['attempt_id']))
        assert done['ok'], done
        assert catalog.reward_points(catalog.get_challenge('tales-trails-2026')) == 3
        preview = scoring.preview_recalculation({'multipliers': {'monthly': 7}}, {'retroactive': False})
        scoring.apply_recalculation(preview, actor='test-admin', reason='Later default', operation_id='later-default')
        init_db(path)
        init_db(path)
        with db_session() as conn:
            assert scoring.user_points(conn, 'old-monthly') == 3
            assert scoring.user_points(conn, 'completed-monthly') == 10
            assert scoring.current_policy(conn)['multipliers']['monthly'] == 7
            assert conn.execute("SELECT COUNT(*) FROM scoring_policies WHERE operation_id='duration-defaults-compressed-v1'").fetchone()[0] == 1
    finally:
        cleanup_db(path)
