"""Status milestone logic (time for all; goal only tadoku)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import get_settings, merge_runtime
from app.services import progress_status as ps

# Ignore operator DB/env toggles during unit tests
def _test_rt():
    rt = merge_runtime(get_settings(), {})
    rt.update(
        status_time_milestones=True,
        status_goal_milestones=True,
        status_final_hour_public=True,
        status_final_hour_dm=True,
        status_updates_enabled=True,
        status_poll_minutes=2.0,
    )
    return rt


ps._runtime = _test_rt  # type: ignore[method-assign]


def _att(**kwargs):
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    base = {
        "id": 1,
        "discord_id": "u1",
        "verify": "tadoku",
        "started_at": now.isoformat(),
        "ends_at": (now + timedelta(hours=8)).isoformat(),
        "title": "Test",
        "tier": "easy",
        "number": 1,
    }
    base.update(kwargs)
    return base, now


def test_time_milestones_short_window_halfway_only():
    """Day-scale windows: 50% only (no 25/75)."""
    att, t0 = _att()  # 8h window
    meta = {"time_hits": [], "goal_hits": []}
    assert ps.check_time_milestones(att, meta, t0 + timedelta(minutes=5)) == []
    # 25% of 8h = 2h — short window skips
    assert ps.check_time_milestones(att, meta, t0 + timedelta(hours=2, minutes=1)) == []
    # halfway
    ev = ps.check_time_milestones(att, meta, t0 + timedelta(hours=4, minutes=1))
    assert len(ev) == 1
    assert ev[0].primary == "Halfway"
    assert ev[0].kind == "status"
    assert "/" in ev[0].secondary  # time bar for card
    assert "hours" in ev[0].secondary
    meta2 = ps.apply_event_marks(meta, ev[0])
    assert ps.check_time_milestones(att, meta2, t0 + timedelta(hours=4, minutes=30)) == []
    # 75% — still skipped on short window
    assert ps.check_time_milestones(att, meta2, t0 + timedelta(hours=6, minutes=1)) == []


def test_time_milestones_long_window_keeps_quartiles():
    att, t0 = _att(ends_at=(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc) + timedelta(days=10)).isoformat())
    meta = {"time_hits": [], "goal_hits": []}
    # 25% of 10d = 2.5d
    ev = ps.check_time_milestones(att, meta, t0 + timedelta(days=2, hours=13))
    assert len(ev) == 1
    assert ev[0].primary == "Quarter in"
    assert "/" in ev[0].secondary


def test_honor_time_copy_no_tadoku_language():
    att, t0 = _att(verify="honor")
    meta = {"time_hits": [], "goal_hits": []}
    ev = ps.check_time_milestones(att, meta, t0 + timedelta(hours=4, minutes=1))
    assert ev
    assert ev[0].primary == "Halfway"
    assert "honor" in ev[0].secondary.lower()
    assert "confirm" in ev[0].secondary.lower()
    assert "tadoku" not in ev[0].secondary.lower()


def test_one_hour_left():
    att, t0 = _att()
    meta = {"time_hits": [0.25, 0.5, 0.75], "goal_hits": []}
    # 50 min left of 8h window
    ev = ps.check_time_milestones(att, meta, t0 + timedelta(hours=7, minutes=10))
    assert any(ps.ONE_H_LEFT in e.mark_time for e in ev)
    final = next(e for e in ev if ps.ONE_H_LEFT in e.mark_time)
    assert final.primary == "Final hour"


def test_due_final_hour_dm_once():
    att, t0 = _att()
    meta: dict = {}
    assert not ps.due_final_hour_dm(att, meta, t0 + timedelta(hours=6))
    assert ps.due_final_hour_dm(att, meta, t0 + timedelta(hours=7, minutes=10))
    marked = ps.mark_final_hour_dm(meta)
    assert marked.get(ps.FINAL_HOUR_DM)
    assert not ps.due_final_hour_dm(att, marked, t0 + timedelta(hours=7, minutes=20))
    # windows ≤1h never DM
    short, t1 = _att(ends_at=(t0 + timedelta(minutes=45)).isoformat())
    assert not ps.due_final_hour_dm(short, {}, t1 + timedelta(minutes=20))
    # already finished → no reminder
    done, _ = _att(status="completed")
    assert not ps.due_final_hour_dm(
        done, {}, t0 + timedelta(hours=7, minutes=10)
    )


def test_goal_milestones_skipped_for_honor():
    import asyncio

    att, t0 = _att(verify="honor", tadoku_user_id="x")
    meta = {"time_hits": [], "goal_hits": [], "goal_met_hint": False}
    ev = asyncio.run(ps.check_goal_milestones(att, meta, t0))
    assert ev == []


def test_pick_events_rate_limit_and_goal_met_bypass():
    att, t0 = _att()
    meta = {
        "time_hits": [],
        "goal_hits": [],
        "last_post_at": t0.isoformat(),
        "goal_met_hint": False,
    }
    time_ev = ps.StatusEvent(
        attempt=att,
        primary="Half",
        secondary="x",
        mark_time=[0.5],
        mark_goal=[],
    )
    # within rate limit — time blocked
    assert ps.pick_events_to_post([time_ev], meta, t0 + timedelta(minutes=2)) == []
    goal_met = ps.StatusEvent(
        attempt=att,
        primary="Goal met",
        secondary="confirm",
        mark_time=[],
        mark_goal=[],
        mark_goal_met=True,
    )
    # goal_met bypasses
    out = ps.pick_events_to_post([time_ev, goal_met], meta, t0 + timedelta(minutes=2))
    assert len(out) == 1 and out[0].mark_goal_met


def test_effective_poll_minutes_budget_scale():
    rt = {"status_poll_minutes": 2.0}
    assert ps.effective_poll_minutes(rt, 1) == 2.0
    assert ps.effective_poll_minutes(rt, 3) == 6.0
    assert ps.effective_poll_minutes(rt, 10) == 15.0  # cap
    assert ps.effective_poll_minutes(rt, 0) == 2.0  # treat as 1


def test_progress_display_from_status_json():
    assert ps.progress_display({"verify": "honor"}) == "—"
    att = {
        "verify": "tadoku",
        "tadoku_user_id": "x",
        "status": "active",
        "goal_amount": 50,
        "goal_kind": "reading",
        "status_json": '{"last_progress": 12.5}',
    }
    assert ps.progress_display(att) == "12.5 / 50 reading"
    snap = ps.progress_snapshot(att)
    assert snap["pct"] == 25
    assert snap["label"] == "12.5 / 50 reading"
    assert ps.card_progress_secondary(att) == "12.5 / 50 reading"
    assert (
        ps.progress_display(
            {
                "verify": "tadoku",
                "tadoku_user_id": "x",
                "status": "active",
                "goal_amount": 720,
                "goal_kind": "minutes",
                "status_json": None,
            }
        )
        == "— / 720 minutes"
    )
    assert ps.progress_snapshot({"verify": "tadoku", "status": "active", "goal_amount": 10}) == {}
