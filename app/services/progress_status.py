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

from app.config import (
    DEFAULT_GOAL_FRACS,
    DEFAULT_TIME_FRACS_LONG,
    DEFAULT_TIME_FRACS_SHORT,
    get_settings,
    merge_runtime,
)
from app.db import db_session, get_all_settings, utcnow_iso
from app.services.verify import parse_iso

logger = logging.getLogger(__name__)

TIME_FRACS_LONG = DEFAULT_TIME_FRACS_LONG
TIME_FRACS_SHORT = DEFAULT_TIME_FRACS_SHORT
TIME_LONG_WINDOW_SEC = 7 * 24 * 3600
GOAL_FRACS = DEFAULT_GOAL_FRACS
ONE_H_LEFT = "1h_left"
FINAL_HOUR_DM = "final_hour_dm"
GOAL_MET = "goal_met"
TIME_FRACS = TIME_FRACS_LONG
# Budget scale: interval = base * N_active, capped (1 user → base, default 2 min)
STATUS_POLL_MAX_MINUTES = 15.0


def _runtime() -> dict[str, Any]:
    try:
        with db_session() as conn:
            return merge_runtime(get_settings(), get_all_settings(conn))
    except Exception:  # noqa: BLE001
        return merge_runtime(get_settings(), {})


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
        result = []
        for row in rows:
            att = dict(row)
            # Existing attempts use accepted rules, not a mutable catalog join.
            try:
                rules = json.loads(att.get("rules_snapshot_json") or "{}")
            except (TypeError, ValueError):
                rules = {}
            for key in ("title", "tier", "number", "verify", "goal_kind", "goal_amount",
                        "media_filter", "param_label", "time_limit_hours", "description", "duration_group"):
                if key in rules:
                    att[key] = rules[key]
            result.append(att)
        return result


def is_tadoku_poll_target(att: dict) -> bool:
    if (att.get("verify") or "tadoku").lower() != "tadoku":
        return False
    return bool((att.get("tadoku_user_id") or "").strip())


def count_tadoku_poll_targets(attempts: list[dict] | None = None) -> int:
    rows = attempts if attempts is not None else list_active_attempts()
    return sum(1 for a in rows if is_tadoku_poll_target(a))


def effective_poll_minutes(
    rt: dict | None = None,
    n_targets: int | None = None,
) -> float:
    """Tadoku poll cadence: base × active targets, clamped to [base, 15].

    ``status_poll_minutes`` is the 1-user interval (default 2).
    """
    rt = rt or _runtime()
    try:
        base = float(rt.get("status_poll_minutes") or 2.0)
    except (TypeError, ValueError):
        base = 2.0
    base = max(1.0, base)
    if n_targets is None:
        n_targets = count_tadoku_poll_targets()
    n = max(1, int(n_targets))
    return min(STATUS_POLL_MAX_MINUTES, base * n)


def _goal_suffix(att: dict) -> tuple[float, str]:
    try:
        goal_f = float(att.get("goal_amount") or 0)
    except (TypeError, ValueError):
        goal_f = 0.0
    kind = (att.get("goal_kind") or "").strip()
    return goal_f, kind


def _fmt_progress(prog_f: float, goal_f: float, kind: str) -> str:
    if goal_f > 0:
        s = f"{prog_f:g} / {goal_f:g}"
        return f"{s} {kind}".strip() if kind else s
    return f"{prog_f:g}"


def _status_meta_from_att(att: dict) -> dict[str, Any]:
    raw = att.get("status_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def progress_snapshot(att: dict) -> dict[str, Any]:
    """Cached goal progress from last Tadoku poll.

    Returns {} or {ratio, pct, label, progress, goal, kind}.
    """
    if (att.get("verify") or "tadoku").lower() != "tadoku":
        return {}
    if (att.get("status") or "").lower() == "scheduled":
        return {}
    goal_f, kind = _goal_suffix(att)
    if goal_f <= 0:
        return {}
    meta = _status_meta_from_att(att)
    if meta.get("evidence_status") == "incomplete":
        return {}
    window = meta.get("evidence_window")
    if window and window != [att.get("started_at"), att.get("ends_at")]:
        return {}
    prog = meta.get("last_progress")
    if prog is None:
        return {}
    try:
        prog_f = float(prog)
    except (TypeError, ValueError):
        return {}
    ratio = prog_f / goal_f
    pct = int(round(100.0 * ratio))
    return {
        "ratio": ratio,
        "pct": pct,
        "label": _fmt_progress(prog_f, goal_f, kind),
        "progress": prog_f,
        "goal": goal_f,
        "kind": kind,
    }


def progress_display(att: dict) -> str:
    """Cached log total for admin tables (from status_json last poll)."""
    if (att.get("verify") or "tadoku").lower() != "tadoku":
        return "—"
    linked = bool((att.get("tadoku_user_id") or "").strip())
    st = (att.get("status") or "").lower()
    goal_f, kind = _goal_suffix(att)
    if _status_meta_from_att(att).get("evidence_status") == "incomplete":
        return "evidence incomplete"
    snap = progress_snapshot(att)
    if snap:
        return str(snap["label"])
    if not linked and st in ("active", "scheduled"):
        return "unlinked"
    if st == "active" and linked:
        # Pending first poll — still show target so column isn't blank
        if goal_f > 0:
            s = f"— / {goal_f:g}"
            return f"{s} {kind}".strip() if kind else s
        return "pending"
    return "—"


def card_progress_secondary(att: dict) -> str:
    """'X / Y unit' for challenge cards (_parse_progress); empty if unknown."""
    snap = progress_snapshot(att)
    return str(snap["label"]) if snap else ""


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
    if abs(frac - 0.25) < 0.03:
        return "Quarter in"
    if abs(frac - 0.5) < 0.03:
        return "Halfway"
    if abs(frac - 0.75) < 0.03:
        return "Three-quarters"
    return f"{int(round(frac * 100))}% time"


def _goal_label(frac: float) -> str:
    if abs(frac - 0.5) < 0.03:
        return "Halfway"
    return f"{int(round(frac * 100))}% goal"


def _final_minutes(rt: dict | None = None) -> float:
    rt = rt or _runtime()
    try:
        m = float(rt.get("status_final_hour_minutes") or 60.0)
    except (TypeError, ValueError):
        m = 60.0
    return max(1.0, m)


def _long_window_sec(rt: dict | None = None) -> float:
    rt = rt or _runtime()
    try:
        d = float(rt.get("status_long_window_days") or 7.0)
    except (TypeError, ValueError):
        d = 7.0
    return max(0.1, d) * 24 * 3600


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
    rt = _runtime()
    if not rt.get("status_time_milestones", True):
        return []
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
    long_sec = _long_window_sec(rt)
    fracs = (
        tuple(rt.get("status_time_fracs_long") or TIME_FRACS_LONG)
        if total >= long_sec
        else tuple(rt.get("status_time_fracs_short") or TIME_FRACS_SHORT)
    )

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

    final_m = _final_minutes(rt)
    final_sec = final_m * 60.0
    if (
        rt.get("status_final_hour_public", True)
        and total > final_sec
        and 0 < remaining <= final_sec
        and ONE_H_LEFT not in hit_set
    ):
        tail = "honor · confirm if you're done" if honor else "final stretch"
        primary = "Final hour" if abs(final_m - 60.0) < 0.5 else f"{int(final_m)}m left"
        events.append(
            StatusEvent(
                attempt=att,
                primary=primary,
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
    poll_minutes: float | None = None,
) -> list[StatusEvent]:
    """Fetch Tadoku progress; optional goal % cards + goal_ok for auto-complete."""
    rt0 = _runtime()
    verify = (att.get("verify") or "tadoku").lower()
    if verify != "tadoku":
        return []
    tadoku_id = (att.get("tadoku_user_id") or "").strip()
    if not tadoku_id:
        return []

    if poll_minutes is not None:
        try:
            poll_m = max(1.0, float(poll_minutes))
        except (TypeError, ValueError):
            poll_m = effective_poll_minutes(rt0)
    else:
        poll_m = effective_poll_minutes(rt0)
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

    rt = rt0
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
        # A previous success must not remain an automatic completion signal
        # after a failed or truncated fetch (especially after a correction).
        meta["goal_ok"] = False
        meta["last_progress"] = None
        meta["evidence_status"] = "incomplete"
        meta["evidence_error"] = str(exc)[:400]
        return []

    meta["evidence_status"] = "complete"
    meta.pop("evidence_error", None)
    meta["evidence_window"] = [att.get("started_at"), att.get("ends_at")]
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

    if rt.get("status_goal_milestones", True):
        goal_fracs = tuple(rt.get("status_goal_fracs") or GOAL_FRACS)
        for m in goal_fracs:
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
    rt = _runtime()
    if not rt.get("status_final_hour_dm", True):
        return False
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
    final_sec = _final_minutes(rt) * 60.0
    if total <= final_sec or remaining <= 0 or remaining > final_sec:
        return False
    return True


def mark_final_hour_dm(meta: dict) -> dict:
    meta = dict(meta)
    meta[FINAL_HOUR_DM] = True
    meta["final_hour_dm_at"] = utcnow_iso()
    return meta


def under_rate_limit(meta: dict, now: datetime | None = None) -> bool:
    """True if we should skip posting due to min interval."""
    rt = _runtime()
    gap = max(1.0, float(rt.get("status_min_interval_minutes") or 12.0)) * 60
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
    poll_minutes: float | None = None,
) -> tuple[list[StatusEvent], dict]:
    """Compute events + updated meta (poll timestamp may be written even if no post)."""
    now = _aware(now or _now())
    aid = int(att["id"])
    meta = get_status_meta(aid)
    window = [att.get("started_at"), att.get("ends_at")]
    if meta.get("effective_window") != window:
        if meta.get("effective_window"):
            meta.setdefault("prior_windows", []).append(meta["effective_window"])
        meta["effective_window"] = window
        meta["deadline_revision"] = att.get("correction_revision", 0)
        meta["goal_ok"] = False
        meta["last_progress"] = None
        meta["last_tadoku_poll_at"] = None
        # Keep sent milestone/DM markers: revising a deadline must not resend
        # notifications already delivered for this attempt.
    events: list[StatusEvent] = []
    events.extend(check_time_milestones(att, meta, now))
    goal_events = await check_goal_milestones(
        att, meta, now, force=force, poll_minutes=poll_minutes
    )
    events.extend(goal_events)
    # Persist poll time from goal check even if no events
    save_status_meta(aid, meta)
    to_post = pick_events_to_post(events, meta, now, force=force)
    return to_post, meta


def tadoku_poll_overview(now: datetime | None = None) -> dict:
    """Admin UI: poll cadence + per-attempt next eligible Tadoku fetch."""
    rt = _runtime()
    now = _aware(now or _now())
    targets = [a for a in list_active_attempts() if is_tadoku_poll_target(a)]
    n_targets = len(targets)
    try:
        base_m = max(1.0, float(rt.get("status_poll_minutes") or 2.0))
    except (TypeError, ValueError):
        base_m = 2.0
    poll_m = effective_poll_minutes(rt, n_targets if n_targets else 1)
    rows: list[dict] = []
    next_at: datetime | None = None
    for att in targets:
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
        "poll_base_minutes": base_m,
        "n_targets": n_targets,
        "tick_seconds": 60,
        "enabled": bool(rt.get("status_updates_enabled")),
        "next_eligible_at": next_at.isoformat() if next_at else None,
        "attempts": rows,
        "now": now.isoformat(),
    }
