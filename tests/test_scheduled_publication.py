from datetime import datetime, timedelta, timezone
import json

from app.catalog import get_challenge, list_challenges
from app.db import db_session
from app.services import attempts, lifecycle
from tests.helpers import setup_tmp_db, cleanup_db


def test_future_publication_hidden_and_future_retirement_not_premature():
    path = setup_tmp_db()
    try:
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        lifecycle.retire_challenge('easy-1', actor='test', reason='Scheduled retirement', effective_at=future, operation_id='retire-future')
        assert 'easy-1' in {c['id'] for c in list_challenges(year=2026)}
        with db_session() as conn:
            conn.execute('UPDATE challenge_defs SET published_at=? WHERE id=?', (future, 'easy-2'))
        assert 'easy-2' not in {c['id'] for c in list_challenges(year=2026)}
        assert get_challenge('easy-2') is not None  # history resolution remains
    finally:
        cleanup_db(path)


def test_retirement_boundary_publishes_prospective_roster():
    path = setup_tmp_db()
    try:
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        lifecycle.retire_challenge('easy-1', actor='test', reason='Scheduled retirement', effective_at=future, operation_id='retire-future')
        with db_session() as conn:
            before = conn.execute("SELECT * FROM daily_rosters WHERE year=2026 AND tier='easy' ORDER BY version DESC LIMIT 1").fetchone()
            assert 'easy-1' in json.loads(before['requirements_json'])
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            conn.execute("UPDATE challenge_defs SET retired_at=? WHERE id='easy-1'", (past,))
        attempts.tick_lifecycle()
        with db_session() as conn:
            after = conn.execute("SELECT * FROM daily_rosters WHERE year=2026 AND tier='easy' ORDER BY version DESC LIMIT 1").fetchone()
            assert after['version'] == before['version'] + 1
            assert 'easy-1' not in json.loads(after['requirements_json'])
    finally:
        cleanup_db(path)
