"""Status milestone logic (time for all; goal only tadoku)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services import progress_status as ps


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


def test_time_milestones_fire_once():
    att, t0 = _att()
    meta = {"time_hits": [], "goal_hits": []}
    # just started — nothing
    ev = ps.check_time_milestones(att, meta, t0 + timedelta(minutes=5))
    assert ev == []
    # 25% of 8h = 2h
    ev = ps.check_time_milestones(att, meta, t0 + timedelta(hours=2, minutes=1))
    assert len(ev) == 1
    assert "25%" in ev[0].primary or "clock" in ev[0].primary.lower()
    meta2 = ps.apply_event_marks(meta, ev[0])
    # again — no re-fire
    ev2 = ps.check_time_milestones(att, meta2, t0 + timedelta(hours=2, minutes=30))
    assert ev2 == []
    # halfway
    ev3 = ps.check_time_milestones(att, meta2, t0 + timedelta(hours=4, minutes=1))
    assert any("Half" in e.primary for e in ev3)


def test_honor_time_copy_no_tadoku_language():
    att, t0 = _att(verify="honor")
    meta = {"time_hits": [], "goal_hits": []}
    ev = ps.check_time_milestones(att, meta, t0 + timedelta(hours=4, minutes=1))
    assert ev
    assert "honor" in ev[0].secondary.lower()
    assert "confirm" in ev[0].secondary.lower()


def test_one_hour_left():
    att, t0 = _att()
    meta = {"time_hits": [0.25, 0.5, 0.75], "goal_hits": []}
    # 50 min left of 8h window
    ev = ps.check_time_milestones(att, meta, t0 + timedelta(hours=7, minutes=10))
    assert any(ps.ONE_H_LEFT in e.mark_time for e in ev)


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
