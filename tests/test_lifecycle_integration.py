"""Isolated end-to-end catalog/attempt lifecycle invariants."""
from datetime import datetime, timezone
import asyncio
import json

from app.catalog import get_challenge, list_challenges, reward_points, upsert_challenges_from_yaml
from app.db import db_session, init_db
from app.services import attempts
from tests.helpers import setup_tmp_db, cleanup_db


def test_tales_month_and_selected_work_snapshot():
    path = setup_tmp_db()
    try:
        ch = get_challenge('tales-trails-2026')
        assert ch['duration_group'] == 'monthly'
        assert ch['points'] is None and reward_points(ch) == 10
        out = attempts.start_attempt('u', challenge_id=ch['id'], param='Tales of Vesperia', guild_id='g')
        assert out['ok'], out
        with db_session() as conn:
            saved = dict(conn.execute('SELECT * FROM attempts WHERE id=?', (out['attempt_id'],)).fetchone())
        assert saved['edition_year'] == 2026
        assert saved['original_deadline'] == saved['ends_at']
        assert json.loads(saved['rules_snapshot_json'])['title'] == ch['title']
        # Mutable catalog edits cannot change accepted verification/deadline.
        with db_session() as conn:
            conn.execute("UPDATE challenge_defs SET verify='tadoku', goal_amount=999, title='Changed' WHERE id=?", (ch['id'],))
        assert attempts.get_attempt(out['attempt_id'])['verify'] == 'honor'
        wrong = asyncio.run(attempts.confirm_attempt('u', attempt_id=out['attempt_id'], content='Trails in the Sky'))
        assert not wrong['ok']
        good = asyncio.run(attempts.confirm_attempt('u', attempt_id=out['attempt_id']))
        assert good['ok'], good
        with db_session() as conn:
            completed = dict(conn.execute('SELECT * FROM attempts WHERE id=?', (out['attempt_id'],)).fetchone())
            original = json.loads(completed['original_completion_json'])
            assert original['status'] == 'completed'
            assert original['verify_method'] == 'honor'
            assert original['evidence_json'] == completed['evidence_json']
            assert original['content'] == completed['content']
            assert original['actual_completed_at'] is None
        assert not attempts.start_attempt('u', challenge_id=ch['id'], param='Trails', guild_id='g')['ok']
    finally:
        cleanup_db(path)


def test_safe_replace_import_and_restart_seed(tmp_path):
    path = setup_tmp_db()
    try:
        with db_session() as conn:
            conn.execute("UPDATE challenge_defs SET title='Admin title' WHERE id='easy-1'")
        upsert_challenges_from_yaml('challenges/2026.yaml', seed_only=True)
        assert get_challenge('easy-1')['title'] == 'Admin title'
        catalog = tmp_path / 'replace.yaml'
        catalog.write_text('year: 2026\nchallenges: []\n')
        upsert_challenges_from_yaml(catalog, replace_year=True)
        assert get_challenge('easy-1')['lifecycle_state'] == 'retired'
        assert not list_challenges(year=2026, tier='easy')
        assert list_challenges(year=2026, tier='easy', include_retired=True)
        init_db(path)
        assert get_challenge('easy-1')['lifecycle_state'] == 'retired'
    finally:
        cleanup_db(path)
