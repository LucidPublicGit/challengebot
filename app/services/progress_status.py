"""Automatic channel status posts for active challenges.

v1:
  - Time milestones: short windows (<7d) 50% + 1h left; long windows 25/50/75% + 1h left
  - Goal milestones (25/50/75% of target + goal-met hint) for verify=tadoku only
  - Honor never gets log/goal-based posts
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings, merge_runtime
from app.db import db_session, get_all_settings, utcnow_iso
from app.services.verify import parse_iso

logger = logging.getLogger(__name__)

TIME_FRACS_LONG = (0.25, 0.5, 0.75)  # >= 7 day windows
TIME_FRACS_SHORT = (0.5,)  # day-scale challenges: halfway only
TIME_LONG_WINDOW_SEC = 7 * 24 * 3600
GOAL_FRACS = (0.25, 0.5, 0.75)
ONE_H_LEFT = "1h_left"
FINAL_HOUR_DM = "final_hour_dm"
GOAL_MET = "goal_met"
# back-compat alias
TIME_FRACS = TIME_FRACS_LONG


def _runtime() -> dict[str, Any]:
    with db_session() as conn:
        return merge_runtime(get_settings(), get_all_settings(conn))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class StatusEvent:
    """One channel status announcement to fire."""

    attempt: dict
    primary: str
    secondary: str
    # keys to mark hit after successful post
    mark_time: list  # floats or ONE_H_LEFT
    mark_goal: list  # floats
    mark_goal_met: bool = False
    kind: str = "status"  # full milestone card (hero + optional bar)


def list_active_attempts() -> list[dict]:
    """All active attempts with challenge + user display fields."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT a.*, c.title, c.tier, c.number, c.verify, c.goal_kind, "
            "c.goal_amount, c.media_filter, c.param_label, c.time_limit_hours, "
            "c.description, "
            "u.display_name, u.tadoku_user_id "
            "FROM attempts a "
            "JOIN challenge_defs c ON c.id = a.challenge_id "
            "LEFT JOIN users u ON u.discord_id = a.discord_id "
            "WHERE a.status = 'active' "
            "ORDER BY a.id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_status_meta(attempt_id: int) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT status_json FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
    if not row or not row["status_json"]:
        return {
            "time_hits": [],
            "goal_hits": [],
            "goal_met_hint": False,
            "goal_ok": False,
            "last_post_at": None,
            "last_tadoku_poll_at": None,
            "last_progress": None,
        }
    try:
        data = json.loads(row["status_json"])
    except json.JSONDecodeError:
        data = {}
    data.setdefault("time_hits", [])
    data.setdefault("goal_hits", [])
    data.setdefault("goal_met_hint", False)
    data.setdefault("goal_ok", False)
    return data


def save_status_meta(attempt_id: int, meta: dict[str, Any]) -> None:
    with db_session() as conn:
        conn.execute(
            "UPDATE attempts SET status_json=? WHERE id=?",
            (json.dumps(meta), attempt_id),
        )


def _fmt_remaining(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h > 0:
        return f"{h}h {m}m left"
    return f"{m}m left"


def _time_label(frac: float) -> str:
    """Short in-card hero (also caption bold)."""
    if frac <= 0.26:
        return "Quarter in"
    if frac <= 0.51:
        return "Halfway"
    return "Three-quarters"


def _goal_label(frac: float) -> str:
    if frac <= 0.26:
        return "25% goal"
    if frac <= 0.51:
        return "Halfway"
    return "75% goal"


def _fmt_qty(x: float) -> str:
    if abs(x - round(x)) < 0.05:
        return str(int(round(x)))
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _time_bar(elapsed: float, total: float, remaining: float, tail: str) -> str:
    """Secondary: `elapsed / total unit · remaining · tail` → card progress bar."""
    if total >= 48 * 3600:
        unit = "days"
        scale = 86400.0
    else:
        unit = "hours"
        scale = 3600.0
    bar = f"{_fmt_qty(elapsed / scale)} / {_fmt_qty(total / scale)} {unit}"
    bits = [bar, _fmt_remaining(remaining)]
    tail = (tail or "").strip(" ·")
    if tail:
        bits.append(tail)
    return " · ".join(bits)


def check_time_milestones(att: dict, meta: dict, now: datetime | None = None) -> list[StatusEvent]:
    """Time-based events for any verify mode (including honor)."""
    now = _aware(now or _now())
    try:
        started = _aware(parse_iso(att["started_at"]))
        ends = _aware(parse_iso(att["ends_at"]))
    except Exception:  # noqa: BLE001
        return []
    total = (ends - started).total_seconds()
    if total <= 60:
        return []
    elapsed = (now - started).total_seconds()
    remaining = (ends - now).total_seconds()
    if remaining <= 0:
        return []
    frac = max(0.0, min(1.0, elapsed / total))
    hits = list(meta.get("time_hits") or [])
    # normalize floats stored as numbers
    hit_set = set()
    for h in hits:
        if h == ONE_H_LEFT:
            hit_set.add(ONE_H_LEFT)
        else:
            try:
                hit_set.add(round(float(h), 2))
            except (TypeError, ValueError):
                pass

    verify = (att.get("verify") or "tadoku").lower()
    honor = verify == "honor"
    events: list[StatusEvent] = []
    fracs = TIME_FRACS_LONG if total >= TIME_LONG_WINDOW_SEC else TIME_FRACS_SHORT

    for m in fracs:
        key = round(float(m), 2)
        if frac + 1e-9 >= m and key not in hit_set:
            hit_set.add(key)
            tail = (
                "honor · still running · confirm when finished"
                if honor
                else "timer checkpoint"
            )
            events.append(
                StatusEvent(
                    attempt=att,
                    primary=_time_label(m),
                    secondary=_time_bar(elapsed, total, remaining, tail),
                    mark_time=[key],
                    mark_goal=[],
                    kind="status",
                )
            )

    # 1 hour left (only if window longer than 1h)
    if (
        total > 3600
        and 0 < remaining <= 3600
        and ONE_H_LEFT not in hit_set
    ):
        tail = "honor · confirm if you're done" if honor else "final stretch"
        events.append(
            StatusEvent(
                attempt=att,
                primary="Final hour",
                secondary=_time_bar(elapsed, total, remaining, tail),
                mark_time=[ONE_H_LEFT],
                mark_goal=[],
                kind="status",
            )
        )

    return events


async def check_goal_milestones(
    att: dict,
    meta: dict,
    now: datetime | None = None,
    *,
    force: bool = False,
) -> list[StatusEvent]:
    """Goal % + goal-met hint. Tadoku-verify + linked only."""
    verify = (att.get("verify") or "tadoku").lower()
    if verify != "tadoku":
        return []
    tadoku_id = (att.get("tadoku_user_id") or "").strip()
    if not tadoku_id:
        return []

    settings = get_settings()
    poll_m = max(1.0, float(settings.status_poll_minutes or 10.0))
    now = _aware(now or _now())
    last_poll = meta.get("last_tadoku_poll_at")
    if last_poll and not force:
        try:
            lp = _aware(parse_iso(str(last_poll)))
            if (now - lp).total_seconds() < poll_m * 60:
                return []
        except Exception:  # noqa: BLE001
            pass

    # Mark poll time even on failure so we don't hammer API
    meta["last_tadoku_poll_at"] = now.isoformat()

    try:
        started = _aware(parse_iso(att["started_at"]))
        ends = _aware(parse_iso(att["ends_at"]))
    except Exception:  # noqa: BLE001
        return []

    rt = _runtime()
    from app.services.verify import evaluate_logs
    from app.tadoku.client import resolve_logs_for_user

    try:
        logs = await resolve_logs_for_user(
            tadoku_user_id=tadoku_id,
            contest_id=str(rt["tadoku_contest_id"]),
            api_base=str(rt["tadoku_api_base"]),
            since=started,
            until=min(now, ends),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "status Tadoku fetch failed attempt=%s: %s", att.get("id"), exc
        )
        return []

    result = evaluate_logs(
        logs,
        goal_kind=att.get("goal_kind") or "",
        goal_amount=float(att.get("goal_amount") or 0),
        media_filter=att.get("media_filter") or "any",
        param=att.get("param"),
        pages_per_volume=float(rt["pages_per_volume"]),
        episode_minutes=float(rt["episode_minutes"]),
    )
    progress = float(result.progress or 0)
    target = float(result.target or 0)
    if target <= 0:
        return []

    meta["last_progress"] = progress
    meta["goal_ok"] = bool(result.ok)
    ratio = progress / target
    hit_set = set()
    for h in meta.get("goal_hits") or []:
        try:
            hit_set.add(round(float(h), 2))
        except (TypeError, ValueError):
            pass

    events: list[StatusEvent] = []
    detail = (result.detail or f"{progress:g} / {target:g} {result.unit_label}").strip()

    for m in GOAL_FRACS:
        key = round(float(m), 2)
        if ratio + 1e-9 >= m and key not in hit_set:
            hit_set.add(key)
            events.append(
                StatusEvent(
                    attempt=att,
                    primary=_goal_label(m),
                    secondary=detail,
                    mark_time=[],
                    mark_goal=[key],
                    kind="status",
                )
            )

    # Bot auto-completes on goal_ok; event is a fallback marker if complete fails
    if result.ok and not meta.get("goal_met_hint"):
        events.append(
            StatusEvent(
                attempt=att,
                primary="Goal met",
                secondary=detail,
                mark_time=[],
                mark_goal=[],
                mark_goal_met=True,
                kind="status",
            )
        )

    return events


def apply_event_marks(meta: dict, event: StatusEvent) -> dict:
    meta = dict(meta)
    th = list(meta.get("time_hits") or [])
    for m in event.mark_time:
        if m not in th:
            th.append(m)
    meta["time_hits"] = th
    gh = list(meta.get("goal_hits") or [])
    for m in event.mark_goal:
        if m not in gh and round(float(m), 2) not in {
            round(float(x), 2) for x in gh if x != ONE_H_LEFT
        }:
            gh.append(round(float(m), 2))
    meta["goal_hits"] = gh
    if event.mark_goal_met:
        meta["goal_met_hint"] = True
    meta["last_post_at"] = utcnow_iso()
    return meta


def attempt_is_active(attempt_id: int) -> bool:
    """Fresh DB check — completed/abandoned/expired must not get reminders."""
    with db_session() as conn:
        row = conn.execute(
            "SELECT status FROM attempts WHERE id=?", (int(attempt_id),)
        ).fetchone()
    return bool(row) and (row["status"] or "") == "active"


def due_final_hour_dm(
    att: dict, meta: dict, now: datetime | None = None
) -> bool:
    """True once when ~1h remains on an still-active attempt (DM not yet sent)."""
    if meta.get(FINAL_HOUR_DM):
        return False
    if (att.get("status") or "active") != "active":
        return False
    now = _aware(now or _now())
    try:
        started = _aware(parse_iso(att["started_at"]))
        ends = _aware(parse_iso(att["ends_at"]))
    except Exception:  # noqa: BLE001
        return False
    total = (ends - started).total_seconds()
    remaining = (ends - now).total_seconds()
    if total <= 3600 or remaining <= 0 or remaining > 3600:
        return False
    return True


def mark_final_hour_dm(meta: dict) -> dict:
    meta = dict(meta)
    meta[FINAL_HOUR_DM] = True
    meta["final_hour_dm_at"] = utcnow_iso()
    return meta


def under_rate_limit(meta: dict, now: datetime | None = None) -> bool:
    """True if we should skip posting due to min interval."""
    settings = get_settings()
    gap = max(1.0, float(settings.status_min_interval_minutes or 12.0)) * 60
    last = meta.get("last_post_at")
    if not last:
        return False
    now = _aware(now or _now())
    try:
        lp = _aware(parse_iso(str(last)))
    except Exception:  # noqa: BLE001
        return False
    return (now - lp).total_seconds() < gap


def pick_events_to_post(
    events: list[StatusEvent],
    meta: dict,
    now: datetime | None = None,
    *,
    force: bool = False,
) -> list[StatusEvent]:
    """Rate-limit: at most one post per tick; goal_met always allowed if not hit."""
    if not events:
        return []
    # Prefer goal_met, then goal milestones, then 1h left, then time
    def prio(ev: StatusEvent) -> int:
        if ev.mark_goal_met:
            return 0
        if ev.mark_goal:
            return 1
        if ONE_H_LEFT in ev.mark_time:
            return 2
        return 3

    events = sorted(events, key=prio)
    # goal_met bypasses rate limit once
    top = events[0]
    if top.mark_goal_met or force:
        return [top]
    if under_rate_limit(meta, now):
        return []
    return [top]


async def collect_status_events_for_attempt(
    att: dict,
    now: datetime | None = None,
    *,
    force: bool = False,
) -> tuple[list[StatusEvent], dict]:
    """Compute events + updated meta (poll timestamp may be written even if no post)."""
    now = _aware(now or _now())
    aid = int(att["id"])
    meta = get_status_meta(aid)
    events: list[StatusEvent] = []
    events.extend(check_time_milestones(att, meta, now))
    goal_events = await check_goal_milestones(att, meta, now, force=force)
    events.extend(goal_events)
    # Persist poll time from goal check even if no events
    save_status_meta(aid, meta)
    to_post = pick_events_to_post(events, meta, now, force=force)
    return to_post, meta


def tadoku_poll_overview(now: datetime | None = None) -> dict:
    """Admin UI: poll cadence + per-attempt next eligible Tadoku fetch."""
    settings = get_settings()
    poll_m = max(1.0, float(settings.status_poll_minutes or 10.0))
    now = _aware(now or _now())
    rows: list[dict] = []
    next_at: datetime | None = None
    for att in list_active_attempts():
        if (att.get("verify") or "tadoku").lower() != "tadoku":
            continue
        if not (att.get("tadoku_user_id") or "").strip():
            continue
        meta = get_status_meta(int(att["id"]))
        last_raw = meta.get("last_tadoku_poll_at")
        last_dt = None
        if last_raw:
            try:
                last_dt = _aware(parse_iso(str(last_raw)))
            except Exception:  # noqa: BLE001
                last_dt = None
        if last_dt is None:
            eligible = now
        else:
            eligible = last_dt + timedelta(minutes=poll_m)
        if eligible < now:
            eligible = now
        if next_at is None or eligible < next_at:
            next_at = eligible
        prog = meta.get("last_progress")
        try:
            prog_f = float(prog) if prog is not None else None
        except (TypeError, ValueError):
            prog_f = None
        rows.append(
            {
                "attempt_id": int(att["id"]),
                "name": att.get("display_name") or att.get("discord_id"),
                "title": att.get("title") or "?",
                "last_poll_at": last_dt.isoformat() if last_dt else None,
                "next_eligible_at": eligible.isoformat(),
                "last_progress": prog_f,
                "goal_amount": float(att.get("goal_amount") or 0) or None,
                "goal_kind": att.get("goal_kind") or "",
            }
        )
    return {
        "poll_minutes": poll_m,
        "tick_seconds": 60,
        "enabled": bool(settings.status_updates_enabled),
        "next_eligible_at": next_at.isoformat() if next_at else None,
        "attempts": rows,
        "now": now.isoformat(),
    }
