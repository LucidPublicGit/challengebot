import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.services import progress_status as ps
from app.tadoku.client import IncompleteLogsError


def test_incomplete_fetch_clears_cached_pass():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    att = {"id": 1, "verify": "tadoku", "tadoku_user_id": "synthetic",
           "started_at": (now - timedelta(days=20)).isoformat(),
           "ends_at": (now + timedelta(days=10)).isoformat()}
    meta = {"goal_ok": True, "last_progress": 100}

    async def incomplete(**kwargs):
        raise IncompleteLogsError("page limit")

    with patch("app.tadoku.client.resolve_logs_for_user", incomplete):
        assert asyncio.run(ps.check_goal_milestones(att, meta, now, force=True, poll_minutes=2)) == []
    assert meta["goal_ok"] is False
    assert meta["last_progress"] is None
    assert meta["evidence_status"] == "incomplete"
    assert ps.progress_display({**att, "status_json": meta}) == "evidence incomplete"


def test_revised_window_invalidates_progress_display():
    att = {"verify": "tadoku", "goal_amount": 10, "started_at": "new", "ends_at": "deadline",
           "status_json": {"last_progress": 8, "evidence_window": ["old", "deadline"]}}
    assert ps.progress_snapshot(att) == {}


def test_deadline_revision_preserves_sent_reminders():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    att = {"id": 1, "verify": "honor", "started_at": (now - timedelta(days=20)).isoformat(),
           "ends_at": (now + timedelta(days=10)).isoformat(), "correction_revision": 2}
    meta = {"effective_window": ["old", "old"], "goal_ok": True, "last_progress": 10,
            "time_hits": [0.25, 0.5, 0.75], "final_hour_dm": True}
    with patch.object(ps, "get_status_meta", return_value=meta), patch.object(ps, "save_status_meta"):
        events, updated = asyncio.run(ps.collect_status_events_for_attempt(att, now))
    assert events == []
    assert updated["time_hits"] == [0.25, 0.5, 0.75]
    assert updated["final_hour_dm"] is True
    assert updated["deadline_revision"] == 2
    assert updated["goal_ok"] is False
