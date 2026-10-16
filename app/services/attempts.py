from __future__ import annotations

import json
import re
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.catalog import (
    get_challenge,
    get_challenge_by_tier_num,
    needs_focus_param,
    reward_points,
    sweep_bonus,
)
from app.config import get_settings, merge_runtime
from app.catalog_changes import record_change
from app.db import (
    COUNTER_MAX,
    SQL_INT_MAX,
    db_session,
    get_all_settings,
    sql_int,
    utcnow_iso,
)
from app.services.verify import (
    VerifyResult,
    evaluate_logs,
    parse_iso,
    resolve_content,
    synthetic_logs_for_goal,
)

_OFFSET_RE = re.compile(
    r"^(?:UTC)?\s*(?P<sign>[+-])\s*(?P<h>\d{1,2})(?::?(?P<m>\d{2}))?$",
    re.I,
)
ATTEMPT_STATUSES = frozenset(
    {"active", "scheduled", "completed", "expired", "abandoned", "void"}
)
_LIST_LIMIT_MAX = 500
_Q_MAX = 200
_HOURS_MAX = 24.0 * 366 * 5


def _bound_hours(raw: Any) -> float:
    try:
        h = float(raw if raw is not None else 24)
    except (TypeError, ValueError):
        h = 24.0
    if h != h or h <= 0:
        h = 24.0
    return min(h, _HOURS_MAX)


def display_content(attempt: dict | None) -> str:
    """Content shown on completed/recent lists (stored content, else param)."""
    if not attempt:
        return ""
    from app.services.verify import format_content_items, parse_content_items

    # Prefer whichever field holds the fuller works list (content was often
    # truncated by old modal limits while note kept the full paste).
    candidates = [
        attempt.get("content") or "",
        attempt.get("note") or "",
        attempt.get("param") or "",
    ]
    best = ""
    best_n = 0
    for raw in candidates:
        raw = (raw or "").strip()
        if not raw:
            continue
        items = parse_content_items(raw)
        n = len(items) if items else (1 if raw else 0)
        if n > best_n or (n == best_n and len(raw) > len(best)):
            best, best_n = raw, n
    if not best:
        return ""
    items = parse_content_items(best)
    if len(items) > 1:
        return format_content_items(best) or best
    return items[0] if items else best
from app.tadoku.client import resolve_logs_for_user


def _runtime() -> dict[str, Any]:
    with db_session() as conn:
        return merge_runtime(get_settings(), get_all_settings(conn))


def ensure_user(discord_id: str, display_name: str | None = None) -> None:
    now = utcnow_iso()
    with db_session() as conn:
        row = conn.execute(
            "SELECT discord_id FROM users WHERE discord_id=?", (discord_id,)
        ).fetchone()
        if row:
            if display_name:
                conn.execute(
                    "UPDATE users SET display_name=?, updated_at=? WHERE discord_id=?",
                    (display_name, now, discord_id),
                )
            return
        conn.execute(
            "INSERT INTO users(discord_id, display_name, created_at, updated_at) "
            "VALUES (?,?,?,?)",
            (discord_id, display_name, now, now),
        )


def link_tadoku(discord_id: str, tadoku_user_id: str, tadoku_name: str | None = None) -> None:
    ensure_user(discord_id)
    now = utcnow_iso()
    with db_session() as conn:
        conn.execute(
            "UPDATE users SET tadoku_user_id=?, tadoku_name=?, updated_at=? WHERE discord_id=?",
            (tadoku_user_id.strip(), tadoku_name, now, discord_id),
        )


def get_user(discord_id: str) -> dict | None:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE discord_id=?", (discord_id,)
        ).fetchone()
        return dict(row) if row else None


def get_tz_offset(discord_id: str, default: str = "+00:00") -> str:
    """Saved wall-clock UTC offset for scheduling (e.g. '-07:00')."""
    user = get_user(discord_id)
    raw = (user or {}).get("tz_offset") if user else None
    off = (raw or "").strip() or default
    return off


def set_tz_offset(discord_id: str, offset: str, display_name: str | None = None) -> None:
    """Remember the offset the user last used for schedule picks."""
    ensure_user(discord_id, display_name)
    off = (offset or "").strip() or "+00:00"
    now = utcnow_iso()
    with db_session() as conn:
        conn.execute(
            "UPDATE users SET tz_offset=?, updated_at=? WHERE discord_id=?",
            (off, now, discord_id),
        )


def _attempt_join_rows(
    discord_id: str, statuses: tuple[str, ...]
) -> list[dict]:
    placeholders = ",".join("?" * len(statuses))
    with db_session() as conn:
        rows = conn.execute(
            "SELECT a.*, c.title, c.tier, c.number, c.verify, c.goal_kind, "
            "c.goal_amount, c.media_filter, c.param_label, c.time_limit_hours, "
            "c.allows_concurrent, c.category "
            "FROM attempts a JOIN challenge_defs c ON c.id = a.challenge_id "
            f"WHERE a.discord_id=? AND a.status IN ({placeholders}) "
            "ORDER BY a.id DESC",
            (discord_id, *statuses),
        ).fetchall()
        return [_captured_rules(dict(r)) for r in rows]


def _captured_rules(attempt: dict) -> dict:
    """Definition fields are accepted at start, not at confirmation time."""
    try:
        rules = json.loads(attempt.get('rules_snapshot_json') or '{}')
        effective = json.loads(attempt.get('effective_completion_json') or '{}')
    except (ValueError, TypeError):
        return attempt
    fields = ('title', 'tier', 'number', 'verify', 'goal_kind', 'goal_amount',
              'media_filter', 'param_label', 'time_limit_hours', 'allows_concurrent',
              'category', 'duration_group', 'repeat_policy', 'points')
    return {**attempt, **{k: effective.get(k, rules[k]) for k in fields if k in rules}}


def _attempt_join_row(discord_id: str, statuses: tuple[str, ...]) -> dict | None:
    rows = _attempt_join_rows(discord_id, statuses)
    return rows[0] if rows else None


def challenge_allows_concurrent(ch: dict | None) -> bool:
    """True when challenge_defs.allows_concurrent is set (opt-in long-term)."""
    if not ch:
        return False
    if ch.get('duration_group') in ('monthly', 'yearly'):
        return True
    raw = ch.get("allows_concurrent")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return False


def active_attempts(discord_id: str) -> list[dict]:
    activate_due_scheduled()
    expire_overdue()
    return _attempt_join_rows(discord_id, ("active",))


def scheduled_attempts(discord_id: str) -> list[dict]:
    activate_due_scheduled()
    return _attempt_join_rows(discord_id, ("scheduled",))


def open_attempts(discord_id: str) -> list[dict]:
    activate_due_scheduled()
    expire_overdue()
    return _attempt_join_rows(discord_id, ("active", "scheduled"))


def active_attempt(discord_id: str) -> dict | None:
    """Newest exclusive active, else newest active (compat single-slot helpers)."""
    acts = active_attempts(discord_id)
    if not acts:
        return None
    exclusive = [a for a in acts if not challenge_allows_concurrent(a)]
    return exclusive[0] if exclusive else acts[0]


def scheduled_attempt(discord_id: str) -> dict | None:
    rows = scheduled_attempts(discord_id)
    return rows[0] if rows else None


def open_attempt(discord_id: str) -> dict | None:
    """Newest exclusive open, else newest open."""
    opens = open_attempts(discord_id)
    if not opens:
        return None
    exclusive = [a for a in opens if not challenge_allows_concurrent(a)]
    return exclusive[0] if exclusive else opens[0]


def _format_open_list(rows: list[dict]) -> str:
    parts = []
    for a in rows:
        focus = display_content(a)
        bit = f"`#{a['id']}` {a.get('tier')} #{a.get('number')} **{a.get('title')}**"
        if focus:
            bit += f" · _{focus}_"
        bit += f" (`{a.get('status')}`)"
        parts.append(bit)
    return "\n".join(parts)


def resolve_user_attempt(
    discord_id: str,
    *,
    attempt_id: int | None = None,
    statuses: tuple[str, ...] = ("active", "scheduled"),
) -> dict[str, Any]:
    """Pick one open attempt; require id when several match."""
    rows = _attempt_join_rows(discord_id, statuses)
    if attempt_id is not None:
        for a in rows:
            if int(a["id"]) == int(attempt_id):
                return {"ok": True, "attempt": a}
        return {
            "ok": False,
            "error": f"No open attempt `#{attempt_id}` for you.",
        }
    if not rows:
        return {"ok": False, "error": "No open challenge."}
    if len(rows) == 1:
        return {"ok": True, "attempt": rows[0]}
    return {
        "ok": False,
        "error": (
            "Several open challenges — pass `attempt_id`:\n"
            + _format_open_list(rows)
        ),
        "attempts": rows,
    }


def parse_schedule_local(
    date_s: str,
    time_s: str,
    offset_s: str = "+00:00",
) -> datetime:
    """
    Parse date+time in a fixed UTC offset → timezone-aware UTC datetime.
    date: YYYY-MM-DD, time: HH:MM or HH:MM:SS, offset: +9 / +09:00 / -5 / UTC+9
    """
    d = (date_s or "").strip()
    t = (time_s or "").strip()
    if not d or not t:
        raise ValueError("Date and time are required.")
    if len(t.split(":")) == 2:
        t = t + ":00"
    raw_off = (offset_s or "+00:00").strip() or "+00:00"
    if raw_off.upper() in ("UTC", "Z", "GMT"):
        sign, oh, om = "+", 0, 0
    else:
        m = _OFFSET_RE.match(raw_off.replace(" ", ""))
        if not m:
            raise ValueError("Offset must look like +9, -5, or +09:00.")
        sign = m.group("sign")
        oh = int(m.group("h"))
        om = int(m.group("m") or 0)
        if oh > 14 or om > 59:
            raise ValueError("Offset out of range.")
    delta = timedelta(hours=oh, minutes=om)
    if sign == "-":
        delta = -delta
    local_tz = timezone(delta)
    local_dt = datetime.fromisoformat(f"{d}T{t}").replace(tzinfo=local_tz)
    return local_dt.astimezone(timezone.utc).replace(microsecond=0)


def get_attempt(attempt_id: int) -> dict | None:
    """Single attempt with challenge + user fields (for admin repost, etc.)."""
    with db_session() as conn:
        row = conn.execute(
            "SELECT a.*, c.title, c.tier, c.number, c.verify, c.goal_kind, "
            "c.goal_amount, c.media_filter, c.param_label, c.time_limit_hours, "
            "u.display_name, u.tadoku_user_id "
            "FROM attempts a "
            "JOIN challenge_defs c ON c.id = a.challenge_id "
            "LEFT JOIN users u ON u.discord_id = a.discord_id "
            "WHERE a.id=?",
            (int(attempt_id),),
        ).fetchone()
        return _captured_rules(dict(row)) if row else None


def _attempts_where(
    discord_id: str | None = None,
    status: str | None = None,
    statuses: list[str] | None = None,
    q: str | None = None,
    challenge_id: str | None = None,
) -> tuple[str, list[Any]]:
    """Shared WHERE clause for list/count attempts."""
    where = " WHERE 1=1"
    args: list[Any] = []
    if discord_id:
        where += " AND a.discord_id=?"
        args.append(discord_id)
    if challenge_id:
        where += " AND a.challenge_id=?"
        args.append(challenge_id)
    if statuses:
        ph = ",".join("?" * len(statuses))
        where += f" AND a.status IN ({ph})"
        args.extend(statuses)
    elif status:
        where += " AND a.status=?"
        args.append(status)
    needle = (q or "").strip()[:_Q_MAX]
    if needle:
        like = f"%{needle}%"
        where += (
            " AND (CAST(a.id AS TEXT) LIKE ? OR a.discord_id LIKE ?"
            " OR IFNULL(u.display_name,'') LIKE ? OR c.id LIKE ?"
            " OR IFNULL(c.title,'') LIKE ? OR IFNULL(a.content,'') LIKE ?"
            " OR IFNULL(a.param,'') LIKE ? OR IFNULL(a.note,'') LIKE ?)"
        )
        args.extend([like] * 8)
    return where, args


def list_attempts(
    discord_id: str | None = None,
    status: str | None = None,
    statuses: list[str] | None = None,
    limit: int | None = 50,
    offset: int | None = None,
    q: str | None = None,
    challenge_id: str | None = None,
) -> list[dict]:
    where, args = _attempts_where(
        discord_id=discord_id,
        status=status,
        statuses=statuses,
        q=q,
        challenge_id=challenge_id,
    )
    sql = (
        "SELECT a.*, c.title, c.tier, c.number, c.verify, c.goal_kind, c.goal_amount, "
        "c.category, u.display_name, u.tadoku_user_id "
        "FROM attempts a "
        "JOIN challenge_defs c ON c.id = a.challenge_id "
        "LEFT JOIN users u ON u.discord_id = a.discord_id"
        f"{where} ORDER BY a.id DESC"
    )
    q_args = list(args)
    if limit is not None:
        sql += " LIMIT ?"
        q_args.append(sql_int(limit, lo=1, hi=_LIST_LIMIT_MAX))
        if offset is not None:
            off = sql_int(offset, lo=0, hi=COUNTER_MAX)
            if off > 0:
                sql += " OFFSET ?"
                q_args.append(off)
    with db_session() as conn:
        return [dict(r) for r in conn.execute(sql, q_args).fetchall()]


def count_attempts(
    discord_id: str | None = None,
    status: str | None = None,
    statuses: list[str] | None = None,
    q: str | None = None,
    challenge_id: str | None = None,
) -> int:
    where, args = _attempts_where(
        discord_id=discord_id,
        status=status,
        statuses=statuses,
        q=q,
        challenge_id=challenge_id,
    )
    sql = (
        "SELECT COUNT(*) AS n FROM attempts a "
        "JOIN challenge_defs c ON c.id = a.challenge_id "
        "LEFT JOIN users u ON u.discord_id = a.discord_id"
        f"{where}"
    )
    with db_session() as conn:
        row = conn.execute(sql, args).fetchone()
        return int(row["n"] if row else 0)


def list_users(*, q: str | None = None, limit: int | None = 500) -> list[dict]:
    """Users with optional search + active attempt count."""
    sql = (
        "SELECT u.*, "
        "(SELECT COUNT(*) FROM attempts a WHERE a.discord_id = u.discord_id "
        " AND a.status IN ('active','scheduled')) AS active_n "
        "FROM users u WHERE 1=1"
    )
    args: list[Any] = []
    needle = (q or "").strip()[:_Q_MAX]
    if needle:
        like = f"%{needle}%"
        sql += (
            " AND (u.discord_id LIKE ? OR IFNULL(u.display_name,'') LIKE ?"
            " OR IFNULL(u.tadoku_user_id,'') LIKE ? OR IFNULL(u.tadoku_name,'') LIKE ?)"
        )
        args.extend([like] * 4)
    sql += " ORDER BY u.updated_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(sql_int(limit, lo=1, hi=_LIST_LIMIT_MAX))
    with db_session() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def attempt_status_counts_for_challenge(challenge_id: str) -> dict[str, int]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM attempts WHERE challenge_id=? GROUP BY status",
            (challenge_id,),
        ).fetchall()
    out = {r["status"]: int(r["n"]) for r in rows}
    out["total"] = sum(out.values())
    return out


def attempt_status_counts(discord_id: str | None = None) -> dict[str, int]:
    """Counts by status (plus total)."""
    q = "SELECT status, COUNT(*) AS n FROM attempts"
    args: list[Any] = []
    if discord_id:
        q += " WHERE discord_id=?"
        args.append(discord_id)
    q += " GROUP BY status"
    with db_session() as conn:
        rows = conn.execute(q, args).fetchall()
    out = {r["status"]: int(r["n"]) for r in rows}
    out["total"] = sum(out.values())
    return out


def expire_overdue() -> list[dict]:
    """Mark overdue actives expired; return those rows (with challenge fields)."""
    now = utcnow_iso()
    with db_session() as conn:
        rows = conn.execute(
            "SELECT a.*, c.title, c.tier, c.number, c.verify, c.goal_kind, "
            "c.goal_amount, c.media_filter, c.param_label, u.display_name "
            "FROM attempts a "
            "JOIN challenge_defs c ON c.id = a.challenge_id "
            "LEFT JOIN users u ON u.discord_id = a.discord_id "
            "WHERE a.status='active' AND a.ends_at <= ?",
            (now,),
        ).fetchall()
        expired = [dict(r) for r in rows]
        if expired:
            conn.execute(
                "UPDATE attempts SET status='expired' "
                "WHERE status='active' AND ends_at <= ?",
                (now,),
            )
            for row in expired:
                _record_transition(conn, row, 'expired', row['ends_at'])
        return expired


def _record_transition(conn, before: dict, status: str, effective_at: str) -> None:
    conn.execute('INSERT INTO attempt_events(attempt_id,event_type,before_json,after_json,effective_at,recorded_at,actor,source,reason) VALUES(?,?,?,?,?,?,?,?,?)',
                 (before['id'], status, json.dumps({'status': before['status']}), json.dumps({'status': status}), effective_at, utcnow_iso(), 'system', 'attempts', 'Attempt lifecycle transition'))


def activate_due_scheduled() -> list[dict]:
    """Flip scheduled attempts whose start time has arrived → active."""
    now = utcnow_iso()
    with db_session() as conn:
        rows = conn.execute(
            "SELECT a.*, c.title, c.tier, c.number, c.verify, c.goal_kind, "
            "c.goal_amount, c.media_filter, c.param_label, u.display_name "
            "FROM attempts a "
            "JOIN challenge_defs c ON c.id = a.challenge_id "
            "LEFT JOIN users u ON u.discord_id = a.discord_id "
            "WHERE a.status='scheduled' AND a.started_at <= ?",
            (now,),
        ).fetchall()
        due = [dict(r) for r in rows]
        if due:
            conn.execute(
                "UPDATE attempts SET status='active' "
                "WHERE status='scheduled' AND started_at <= ?",
                (now,),
            )
            for row in due:
                from app.services import scoring
                raw = conn.execute('SELECT * FROM challenge_defs WHERE id=?', (row['challenge_id'],)).fetchone()
                accepted = json.loads(row.get('rules_snapshot_json') or '{}')
                score_rules = {**dict(raw), **accepted, 'points': raw['points']}
                previous_score = json.loads(row.get('scoring_snapshot_json') or '{}')
                explicit = previous_score.get('explicit_scoring_revision') or {}
                # A reviewed include-open recalculation performed after the
                # actual start must survive delayed scheduler activation.
                preserve_explicit = explicit.get('effective_at') and parse_iso(explicit['effective_at']) >= parse_iso(row['started_at'])
                score = previous_score if preserve_explicit else scoring.capture_snapshot(conn, score_rules, at=row['started_at'])
                score_json = row['scoring_snapshot_json'] if preserve_explicit else json.dumps(score)
                conn.execute('INSERT INTO attempt_events(attempt_id,event_type,before_json,after_json,effective_at,recorded_at,actor,source,reason) VALUES(?,?,?,?,?,?,?,?,?)',
                             (row['id'], 'scheduled_scoring_cutover', row.get('scoring_snapshot_json'), score_json, row['started_at'], now, 'system', 'scheduler', 'Scoring entitlement captured at actual scheduled start; original advertised policy retained here'))
                conn.execute('UPDATE attempts SET scoring_snapshot_json=? WHERE id=?', (score_json, row['id']))
                _record_transition(conn, row, 'active', row['started_at'])
                row["status"] = "active"
        return due


def tick_lifecycle() -> tuple[list[dict], list[dict]]:
    """Publish effective availability boundaries, activate schedules, expire actives."""
    # Scheduled retirement/publication must publish its prospective Daily roster
    # at the effective boundary, even if nobody edits the catalog that day.
    from app.services.badges import publish_rosters
    from app.services.lifecycle import materialize_restorations
    with db_session() as conn:
        materialize_restorations(conn)
        publish_rosters(conn)
    activated = activate_due_scheduled()
    expired = expire_overdue()
    return activated, expired


def start_attempt(
    discord_id: str,
    *,
    tier: str | None = None,
    number: int | None = None,
    challenge_id: str | None = None,
    param: str | None,
    guild_id: str | None,
    display_name: str | None = None,
    year: int = 2026,
    starts_at: datetime | None = None,
) -> dict[str, Any]:
    ensure_user(discord_id, display_name)
    activate_due_scheduled()
    expire_overdue()
    rt = _runtime()
    ch = None
    if challenge_id:
        ch = get_challenge(challenge_id.strip())
        if not ch:
            return {"ok": False, "error": f"Unknown challenge `{challenge_id}`."}
    else:
        if not tier or number is None:
            return {"ok": False, "error": "Pick a specific challenge."}
        ch = get_challenge_by_tier_num(tier, int(number), year=year)
        if not ch:
            return {"ok": False, "error": f"No challenge {tier} #{number} for {year}."}

    if needs_focus_param(ch) and not (param and param.strip()):
        return {
            "ok": False,
            "error": f"This challenge requires **{ch['param_label']}**.",
        }

    focus = (param or "").strip() or None
    if not focus:
        fixed = (ch.get("fixed_param") or "").strip() or None
        if fixed:
            focus = fixed

    opens = open_attempts(discord_id)
    if any(o.get("challenge_id") == ch["id"] for o in opens):
        hit = next(o for o in opens if o.get("challenge_id") == ch["id"])
        return {
            "ok": False,
            "error": (
                f"You already have **{hit['title']}** open "
                f"(`{hit.get('status')}`, attempt `#{hit['id']}`)."
            ),
        }

    new_concurrent = challenge_allows_concurrent(ch)
    if not new_concurrent:
        exclusive = [o for o in opens if not challenge_allows_concurrent(o)]
        if exclusive:
            existing = exclusive[0]
            st = existing.get("status")
            if st == "scheduled":
                when = int(parse_iso(existing["started_at"]).timestamp())
                return {
                    "ok": False,
                    "error": (
                        f"You already scheduled **{existing['title']}** "
                        f"(starts <t:{when}:R>). `/challenge abandon` to cancel, "
                        f"or start a long concurrent challenge instead."
                    ),
                }
            return {
                "ok": False,
                "error": (
                    f"You already have an exclusive challenge: **{existing['title']}** "
                    f"(ends <t:{int(parse_iso(existing['ends_at']).timestamp())}:R>). "
                    f"Abandon it, or start a long concurrent challenge alongside."
                ),
            }

    if ch.get('repeat_policy', 'legacy') == 'legacy' and not rt["allow_reattempt"]:
        with db_session() as conn:
            done = conn.execute(
                "SELECT id FROM attempts WHERE discord_id=? AND challenge_id=? "
                "AND status='completed' LIMIT 1",
                (discord_id, ch["id"]),
            ).fetchone()
        if done:
            return {
                "ok": False,
                "error": "You already cleared this challenge — each one is one-time only.",
            }

    if ch.get("verify") == "tadoku":
        user = get_user(discord_id)
        if not user or not user.get("tadoku_user_id"):
            return {
                "ok": False,
                "error": "Link your Tadoku account first: `/challenge link` (or use **Link Tadoku…** on the start panel).",
            }

    now = datetime.now(timezone.utc).replace(microsecond=0)
    start_dt = now
    if starts_at is not None:
        start_dt = starts_at
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        else:
            start_dt = start_dt.astimezone(timezone.utc)
        start_dt = start_dt.replace(microsecond=0)
        # Past / near-now → start immediately
        if start_dt <= now + timedelta(seconds=30):
            start_dt = now

    from app.services.lifecycle import calculate_deadline, validate_start, capture_attempt, ensure_catalog_records
    try:
        ends = calculate_deadline(ch, start_dt)
    except ValueError as exc:
        return {'ok': False, 'error': str(exc)}
    status = "active" if start_dt <= now else "scheduled"
    with db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        ensure_catalog_records(conn)
        ch = dict(conn.execute('SELECT * FROM challenge_defs WHERE id=?', (ch['id'],)).fetchone())
        try:
            validate_start(conn, ch, discord_id, start_dt)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}
        # Recheck the legacy exception in the same write transaction as insertion.
        if ch.get('repeat_policy') == 'legacy' and not rt['allow_reattempt']:
            if conn.execute("SELECT 1 FROM attempts WHERE discord_id=? AND challenge_id=? AND status='completed'", (discord_id, ch['id'])).fetchone():
                return {'ok': False, 'error': 'You already cleared this challenge — each one is one-time only.'}
        ends = calculate_deadline(ch, start_dt)
        cur = conn.execute(
            "INSERT INTO attempts("
            "discord_id, challenge_id, param, status, started_at, ends_at, guild_id"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                discord_id,
                ch["id"],
                focus,
                status,
                start_dt.isoformat(),
                ends.isoformat(),
                guild_id,
            ),
        )
        attempt_id = cur.lastrowid
        capture_attempt(conn, attempt_id, ch)

    return {
        "ok": True,
        "attempt_id": attempt_id,
        "challenge": ch,
        "status": status,
        "started_at": start_dt.isoformat(),
        "ends_at": ends.isoformat(),
        "param": focus,
    }


def abandon_attempt(
    discord_id: str, *, attempt_id: int | None = None
) -> dict[str, Any]:
    picked = resolve_user_attempt(
        discord_id, attempt_id=attempt_id, statuses=("active", "scheduled")
    )
    if not picked.get("ok"):
        err = picked.get("error") or "No active or scheduled challenge."
        if err == "No open challenge.":
            err = "No active or scheduled challenge."
        return {"ok": False, "error": err, "attempts": picked.get("attempts")}
    att = picked["attempt"]
    was_active = (att.get("status") or "") == "active"
    with db_session() as conn:
        if was_active:
            conn.execute(
                "UPDATE attempts SET status='abandoned', verify_method='quit' "
                "WHERE id=? AND status='active'",
                (att["id"],),
            )
        else:
            conn.execute(
                "UPDATE attempts SET status='abandoned' "
                "WHERE id=? AND status='scheduled'",
                (att["id"],),
            )
        _record_transition(conn, att, 'abandoned' if was_active else 'cancelled', utcnow_iso())
    out = {**att, "status": "abandoned"}
    if was_active:
        out["verify_method"] = "quit"
    return {"ok": True, "attempt": out, "fail": was_active}


def _parse_exclude(raw: str | None) -> list[str]:
    if not raw or not str(raw).strip():
        return []
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def _parse_fixup(raw: str | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"aliases": [], "log_ids": []}
    if not raw or not str(raw).strip():
        return out
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return out
    if not isinstance(data, dict):
        return out
    for key in ("aliases", "log_ids"):
        val = data.get(key) or []
        if isinstance(val, str):
            val = _parse_exclude(val)
        out[key] = [str(x).strip() for x in val if str(x).strip()]
    return out


def _dump_fixup(aliases: list[str], log_ids: list[str]) -> str | None:
    aliases = [a for a in aliases if a]
    log_ids = [i for i in log_ids if i]
    if not aliases and not log_ids:
        return None
    return json.dumps({"aliases": aliases, "log_ids": log_ids}, ensure_ascii=False)


def _merge_unique(*lists: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for x in lst:
            k = str(x).strip()
            if not k or k.lower() in seen:
                continue
            seen.add(k.lower())
            out.append(k)
    return out


def user_update_attempt_content(
    discord_id: str,
    *,
    attempt_id: int | None = None,
    content: str | None = None,
    remove: str | None = None,
    param: str | None = None,
    clear_param: bool = False,
    aliases: str | None = None,
    log_ids: str | None = None,
    clear_fixup: bool = False,
    reason: str = 'Owner content/focus/match correction',
) -> dict[str, Any]:
    """Audited owner edit of content/param/Tadoku fixup on own attempt (any status)."""
    from app.services.verify import strip_content_labels

    q = (
        "SELECT a.*, c.title, c.tier, c.number FROM attempts a "
        "JOIN challenge_defs c ON c.id = a.challenge_id WHERE a.discord_id=?"
    )
    args: list[Any] = [discord_id]
    if attempt_id is not None:
        q += " AND a.id=?"
        args.append(int(attempt_id))
    else:
        q += " AND a.status IN ('active','scheduled','completed','expired','abandoned') "
        q += "ORDER BY a.id DESC LIMIT 1"
    if not str(reason or '').strip():
        return {'ok': False, 'error': 'An audit reason is required.'}
    with db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(q, args).fetchone()
        if not row:
            return {"ok": False, "error": "Attempt not found."}
        att = dict(row)
        new_content = att.get("content")
        if content is not None:
            new_content = (content or "").strip() or None
        if remove:
            new_content = strip_content_labels(
                new_content or att.get("param"), _parse_exclude(remove)
            ) or None
        new_param = att.get("param")
        if clear_param:
            new_param = None
        elif param is not None:
            new_param = (param or "").strip() or None

        fix = _parse_fixup(att.get("fixup_json"))
        if clear_fixup:
            fix = {"aliases": [], "log_ids": []}
        if aliases is not None:
            # replace aliases list when provided (empty clears)
            fix["aliases"] = _parse_exclude(aliases)
        if log_ids is not None:
            fix["log_ids"] = _parse_exclude(log_ids)
        fixup_s = _dump_fixup(fix["aliases"], fix["log_ids"])

        if (new_content, new_param, fixup_s) != (att.get('content'), att.get('param'), att.get('fixup_json')):
            from app.services.lifecycle import _object
            import uuid
            operation_id = f'owner-correction:{uuid.uuid4().hex}'
            meta = _object(att.get('effective_completion_json'))
            meta.update(content=new_content, param=new_param, fixup_json=fixup_s)
            reminders = _object(att.get('status_json'))
            reminders.update(goal_ok=False, last_progress=None, last_tadoku_poll_at=None)
            conn.execute(
                "UPDATE attempts SET content=?, param=?, fixup_json=?, correction_revision=correction_revision+1, "
                "effective_completion_json=?, status_json=?, "
                "original_completion_json=CASE WHEN status='completed' THEN COALESCE(original_completion_json,?) ELSE original_completion_json END "
                "WHERE id=? AND discord_id=?",
                (new_content, new_param, fixup_s, json.dumps(meta), json.dumps(reminders), json.dumps(att), att['id'], discord_id),
            )
            current = dict(conn.execute('SELECT * FROM attempts WHERE id=?', (att['id'],)).fetchone())
            conn.execute('INSERT INTO attempt_events(attempt_id,event_type,before_json,after_json,effective_at,recorded_at,actor,source,reason,operation_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (att['id'], 'owner_content_correction', json.dumps(att), json.dumps(current), utcnow_iso(), utcnow_iso(), str(discord_id), 'discord_user', reason.strip(), operation_id))
    return {
        "ok": True,
        "attempt_id": att["id"],
        "content": new_content,
        "param": new_param,
        "aliases": fix["aliases"],
        "log_ids": fix["log_ids"],
        "title": att.get("title"),
    }


async def confirm_attempt(
    discord_id: str,
    *,
    attempt_id: int | None = None,
    note: str | None = None,
    content: str | None = None,
    ignore_param: bool = False,
    param_override: str | None = None,
    exclude: str | None = None,
    aliases: str | None = None,
    log_ids: str | None = None,
    honor_override: bool = False,
    force_honor: bool = False,
    dry_run: bool = False,
    fake_logs: list[dict[str, Any]] | None = None,
    method_override: str | None = None,
) -> dict[str, Any]:
    """
    Confirm active attempt. Tadoku challenges must pass verify unless
    force_honor (admin) or honor_override (user backdoor when enabled).

    dry_run: evaluate only, never write completed.
    fake_logs: admin test inject — skip Tadoku fetch, run evaluate_logs on these.
    """
    if not dry_run and (force_honor or fake_logs is not None):
        return {'ok': False, 'error': 'Administrative and synthetic completions require the reviewed correction workflow.'}
    if not dry_run:
        activate_due_scheduled()
        expire_overdue()
    if attempt_id is None:
        only_sched = _attempt_join_rows(discord_id, ('scheduled',)) if dry_run else scheduled_attempts(discord_id)
        actives = _attempt_join_rows(discord_id, ('active',)) if dry_run else active_attempts(discord_id)
        if not actives and only_sched:
            if len(only_sched) == 1:
                scheduled = only_sched[0]
                when = int(parse_iso(scheduled["started_at"]).timestamp())
                return {
                    "ok": False,
                    "error": (
                        f"**{scheduled['title']}** is scheduled to start <t:{when}:R>. "
                        "Wait until then, or `/challenge abandon` to cancel."
                    ),
                    "attempt": scheduled,
                }
            return {
                "ok": False,
                "error": (
                    "Only scheduled challenges — nothing to confirm yet:\n"
                    + _format_open_list(only_sched)
                ),
                "attempts": only_sched,
            }
    picked = resolve_user_attempt(
        discord_id, attempt_id=attempt_id, statuses=("active",)
    )
    if not picked.get("ok"):
        err = picked.get("error") or "No active challenge."
        if err == "No open challenge.":
            err = "No active challenge (or it already expired)."
        return {
            "ok": False,
            "error": err,
            "attempts": picked.get("attempts"),
        }
    att = picked["attempt"]

    now = datetime.now(timezone.utc)
    ends = parse_iso(att["ends_at"])
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    if now >= ends:
        if not dry_run:
            expire_overdue()
        return {'ok': False, 'dry_run': dry_run, 'would_complete': False,
                'error': 'Challenge window has ended. Use a reviewed correction to attest an earlier completion.'}

    ch_verify = att.get("verify") or "tadoku"
    rt = _runtime()
    exclude_list = _parse_exclude(exclude)
    user_content = (content or "").strip() or None
    stored_fix = _parse_fixup(att.get("fixup_json"))
    alias_list = _merge_unique(
        stored_fix.get("aliases") or [],
        _parse_exclude(aliases),
    )
    include_ids = _merge_unique(
        stored_fix.get("log_ids") or [],
        _parse_exclude(log_ids),
    )

    if att.get('duration_group') in ('monthly', 'yearly') and att.get('goal_kind') == 'complete_work':
        selected = str(att.get('param') or '').strip()
        supplied = str(param_override if param_override is not None else content or selected).strip()
        if supplied != selected:
            return {'ok': False, 'error': 'The selected work is captured at start. Use an audited correction to change its title.', 'attempt': att}
    if param_override is not None:
        verify_param = (param_override or "").strip() or None
    elif ignore_param:
        verify_param = None
    else:
        verify_param = att.get("param")

    # Persist any new aliases / log ids provided on this confirm
    if (aliases is not None or log_ids is not None) and not dry_run:
        user_update_attempt_content(discord_id, attempt_id=att['id'],
                                    aliases=','.join(alias_list), log_ids=','.join(include_ids),
                                    reason='Verification matching corrections supplied during confirmation')
        att = get_attempt(att['id']) or att

    use_honor = force_honor or (
        honor_override and bool(rt.get("allow_honor_override", True))
    )
    if honor_override and not rt.get("allow_honor_override", True) and not force_honor:
        return {
            "ok": False,
            "error": "Honor override is disabled by admins.",
            "attempt": att,
        }

    # Honor complete_work / honor_override should name what finished.
    needs_named_content = (
        (
            ch_verify == "honor"
            and (att.get("goal_kind") or "") == "complete_work"
        )
        or (honor_override and not force_honor)
    )
    if (
        needs_named_content
        and not user_content
        and not (att.get("param") or "").strip()
        and not force_honor
        and fake_logs is None
    ):
        return {
            "ok": False,
            "error": (
                "Enter what you completed (series, book, or YouTube URL) "
                "in the confirm content field."
            ),
            "attempt": att,
        }

    def _honor_content() -> str | None:
        # Merge content + note so a long paste in either field is not lost
        from app.services.verify import format_content_items, parse_content_items

        merged_parts: list[str] = []
        for blob in (user_content, note, att.get("content"), att.get("note")):
            for it in parse_content_items(blob):
                if it not in merged_parts:
                    merged_parts.append(it)
        merged = format_content_items("\n".join(merged_parts)) if merged_parts else None
        return resolve_content(
            param=att.get("param"),
            note=note,
            content_override=merged or user_content,
            exclude=exclude_list,
        )

    if use_honor and not fake_logs:
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "would_complete": True,
                "method": method_override or "manual",
                "attempt": att,
                "note": note,
                "content": _honor_content(),
            }
        method = method_override or (
            "honor"
            if ch_verify == "honor" and not honor_override
            else ("honor_override" if honor_override and not force_honor else "manual")
        )
        return _complete(
            att,
            method=method,
            evidence={
                "note": note,
                "forced": force_honor,
                "honor_override": bool(honor_override),
                "ignore_param": ignore_param,
                "exclude": exclude_list,
                "aliases": alias_list,
                "log_ids": include_ids,
            },
            note=note,
            content=_honor_content(),
        )

    if ch_verify == "honor" and fake_logs is None and not force_honor:
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "would_complete": True,
                "method": "honor",
                "attempt": att,
                "progress": "Honor challenge — confirm would succeed without Tadoku.",
                "content": _honor_content(),
            }
        return _complete(
            att,
            method="honor",
            evidence={
                "note": note,
                "forced": False,
                "exclude": exclude_list,
            },
            note=note,
            content=_honor_content(),
        )

    user = get_user(discord_id)
    logs: list[dict[str, Any]]
    source = "tadoku"

    if fake_logs is not None:
        logs = list(fake_logs)
        source = "fake"
    else:
        if not user or not user.get("tadoku_user_id"):
            tip = ""
            if rt.get("allow_honor_override", True):
                tip = (
                    " Link Tadoku, or `/challenge failoverride` "
                    "(honor complete, no admin)."
                )
            return {
                "ok": False,
                "error": f"Link Tadoku first (`/challenge link`).{tip}",
                "attempt": att,
            }
        started = parse_iso(att["started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        try:
            logs = await resolve_logs_for_user(
                tadoku_user_id=user["tadoku_user_id"],
                contest_id=str(rt["tadoku_contest_id"]),
                api_base=str(rt["tadoku_api_base"]),
                since=started,
                until=min(now, ends),
            )
        except Exception as exc:  # noqa: BLE001
            tip = ""
            if rt.get("allow_honor_override", True):
                tip = (
                    " Use `/challenge failoverride`, or fix aliases/log ids "
                    "via `/challenge match` / `/challenge logs`."
                )
            return {
                "ok": False,
                "error": f"Tadoku fetch failed: {type(exc).__name__}: {exc}.{tip}",
                "attempt": att,
            }

    result = evaluate_logs(
        logs,
        goal_kind=att["goal_kind"],
        goal_amount=float(att["goal_amount"]),
        media_filter=att.get("media_filter") or "any",
        param=verify_param,
        exclude=exclude_list or None,
        aliases=alias_list or None,
        include_log_ids=include_ids or None,
        pages_per_volume=float(rt["pages_per_volume"]),
        episode_minutes=float(rt["episode_minutes"]),
    )
    payload = {
        "progress": result.detail,
        "verify": _result_dict(result),
        "log_count": len(logs),
        "log_source": source,
        "attempt": att,
        "tadoku_user_id": (user or {}).get("tadoku_user_id"),
        "verify_param": verify_param,
        "ignore_param": ignore_param,
        "aliases": alias_list,
        "log_ids": include_ids,
    }
    if not result.ok:
        tips = [
            "Fix titles: `/challenge logs` + `/challenge match` "
            "(aliases / log ids), or clear focus with "
            "`/challenge content clear_param:true`, then confirm again.",
        ]
        if rt.get("allow_honor_override", True):
            tips.append(
                "**Backdoor (no admin):** `/challenge failoverride` — "
                "honor-complete if Tadoku is wrong or missing."
            )
        err = (
            "Tadoku logs do not meet the goal yet."
            if source == "tadoku"
            else "Fake logs do not meet the goal."
        )
        return {
            "ok": False,
            "error": err + "\n" + "\n".join(tips),
            "dry_run": dry_run,
            **payload,
        }
    content_val = resolve_content(
        param=verify_param or att.get("param"),
        logs=logs,
        matched_ids=result.matched_log_ids,
        note=note,
        content_override=user_content,
        exclude=exclude_list or None,
    )
    payload["content"] = content_val
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "would_complete": True,
            "method": method_override
            or ("tadoku_poll" if source == "tadoku" else "test_fake"),
            **payload,
        }
    return _complete(
        att,
        method=method_override
        or ("tadoku_poll" if source == "tadoku" else "test_fake"),
        evidence={
            "note": note,
            "verify": _result_dict(result),
            "log_count": len(logs),
            "log_source": source,
            "tadoku_user_id": (user or {}).get("tadoku_user_id"),
            "test": source == "fake",
            "content": content_val,
            "verify_param": verify_param,
            "ignore_param": ignore_param,
            "exclude": exclude_list,
            "aliases": alias_list,
            "log_ids": include_ids,
        },
        note=note,
        verify_result=result,
        logs=logs,
        content=content_val,
    )


async def preview_attempt_logs(
    discord_id: str,
    *,
    attempt_id: int | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    """List window Tadoku logs (id + series) so users can link fixups."""
    picked = resolve_user_attempt(
        discord_id, attempt_id=attempt_id, statuses=("active",)
    )
    if not picked.get("ok"):
        return {"ok": False, "error": picked.get("error") or "No active challenge."}
    att = picked["attempt"]
    user = get_user(discord_id)
    if not user or not user.get("tadoku_user_id"):
        return {"ok": False, "error": "Link Tadoku first."}
    rt = _runtime()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    started = parse_iso(att["started_at"])
    ends = parse_iso(att["ends_at"])
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    try:
        logs = await resolve_logs_for_user(
            tadoku_user_id=user["tadoku_user_id"],
            contest_id=str(rt["tadoku_contest_id"]),
            api_base=str(rt["tadoku_api_base"]),
            since=started,
            until=min(now, ends),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Tadoku fetch failed: {exc}"}
    fix = _parse_fixup(att.get("fixup_json"))
    rows = []
    for log in logs[: max(1, int(limit))]:
        series = (
            str(log.get("series") or "").strip()
            or str(log.get("description") or "")[:60]
            or str(log.get("title") or "")
        )
        rows.append(
            {
                "id": str(log.get("id") or ""),
                "series": series,
                "amount": log.get("amount"),
                "unit": log.get("unit") or log.get("unit_name"),
                "tags": log.get("tags"),
            }
        )
    return {
        "ok": True,
        "attempt": att,
        "log_count": len(logs),
        "logs": rows,
        "aliases": fix.get("aliases") or [],
        "log_ids": fix.get("log_ids") or [],
        "param": att.get("param"),
    }


async def probe_tadoku_link(
    discord_id: str | None = None,
    *,
    tadoku_user_id: str | None = None,
    hours_back: float = 48.0,
) -> dict[str, Any]:
    """Fetch recent Tadoku logs for a linked user or raw tadoku id (admin test)."""
    rt = _runtime()
    uid = (tadoku_user_id or "").strip() or None
    user = get_user(discord_id) if discord_id else None
    if not uid:
        if not user or not user.get("tadoku_user_id"):
            return {
                "ok": False,
                "error": "No Tadoku id — link the user or pass tadoku_user_id.",
                "user": user,
            }
        uid = user["tadoku_user_id"]
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours_back)
    try:
        logs = await resolve_logs_for_user(
            tadoku_user_id=uid,
            contest_id=str(rt["tadoku_contest_id"]),
            api_base=str(rt["tadoku_api_base"]),
            since=since,
            until=until,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Tadoku fetch failed: {type(exc).__name__}: {exc}"}
    samples = []
    for log in logs[:5]:
        samples.append(
            {
                "id": log.get("id"),
                "created_at": log.get("created_at"),
                "amount": log.get("amount"),
                "unit": log.get("unit") or log.get("unit_name"),
                "tags": log.get("tags"),
                "description": (log.get("description") or "")[:80],
            }
        )
    return {
        "ok": True,
        "tadoku_user_id": uid,
        "discord_user": user,
        "contest_id": rt["tadoku_contest_id"],
        "window_hours": hours_back,
        "log_count": len(logs),
        "samples": samples,
    }


async def admin_test_verify(
    discord_id: str,
    *,
    mode: str = "preview_real",
    apply: bool = False,
    note: str | None = None,
) -> dict[str, Any]:
    """
    Admin test harness.

    mode:
      preview_real — dry-run with live Tadoku logs
      fake_pass    — synthetic logs that should pass
      fake_fail    — synthetic logs that should fail
      force        — honor/manual complete without Tadoku
    Diagnostic only: apply=True is rejected. Real administrative completions
    require an explicit completion-time attestation and reviewed correction.
    Existing historical synthetic awards remain readable and are not erased.
    """
    if apply:
        return {'ok': False, 'dry_run': True, 'error': 'Diagnostics cannot award completions. Use the reviewed administrative attestation workflow.'}
    mode = (mode or "preview_real").lower().strip()
    active_rows = _attempt_join_rows(discord_id, ('active',))
    att = active_rows[0] if active_rows else None
    if not att and mode != "force":
        # force still needs active attempt via confirm_attempt
        pass

    rt = _runtime()
    if mode == "preview_real":
        return await confirm_attempt(discord_id, note=note, dry_run=not apply)
    if mode == "force":
        return await confirm_attempt(
            discord_id,
            note=note or "admin test force",
            force_honor=True,
            dry_run=not apply,
            method_override="test_force",
        )
    if mode in ("fake_pass", "fake_fail"):
        if not att:
            return {"ok": False, "error": "No active challenge to test against."}
        if (att.get("verify") or "") == "honor" and mode == "fake_pass":
            # honor challenges don't use logs
            return await confirm_attempt(
                discord_id,
                note=note or "admin test honor",
                force_honor=True,
                dry_run=not apply,
                method_override="test_force",
            )
        logs = synthetic_logs_for_goal(
            goal_kind=att["goal_kind"],
            goal_amount=float(att["goal_amount"]),
            media_filter=att.get("media_filter") or "any",
            param=att.get("param"),
            meet_goal=(mode == "fake_pass"),
            pages_per_volume=float(rt["pages_per_volume"]),
            episode_minutes=float(rt["episode_minutes"]),
        )
        return await confirm_attempt(
            discord_id,
            note=note or f"admin test {mode}",
            dry_run=not apply,
            fake_logs=logs,
            method_override="test_fake",
        )
    return {"ok": False, "error": f"Unknown mode: {mode}"}


def _result_dict(r: VerifyResult) -> dict:
    return {
        "ok": r.ok,
        "progress": r.progress,
        "target": r.target,
        "unit_label": r.unit_label,
        "detail": r.detail,
        "matched_log_ids": r.matched_log_ids,
        "method": r.method,
    }


def _complete(
    att: dict,
    *,
    method: str,
    evidence: dict,
    note: str | None,
    verify_result: VerifyResult | None = None,
    logs: list[dict[str, Any]] | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    # Start instants retain subsecond precision. Truncating confirmation to a
    # whole second could otherwise put an immediate clear before its start.
    now = datetime.now(timezone.utc).isoformat()
    matched = (verify_result.matched_log_ids if verify_result else None) or []
    content_val = content or resolve_content(
        param=att.get("param"),
        logs=logs,
        matched_ids=matched,
        note=note,
    )
    # Prefer fuller works list if note still has more lines than content_val
    from app.services.verify import format_content_items, parse_content_items

    note_items = parse_content_items(note)
    content_items = parse_content_items(content_val)
    if len(note_items) > len(content_items):
        content_val = format_content_items(note) or content_val
    elif content_val:
        content_val = format_content_items(content_val) or content_val
    evidence = {**evidence, "content": content_val}
    with db_session() as conn:
        from app.services.achievements import completion_snapshot, sync_user

        if evidence.get("log_source") == "tadoku":
            binding = conn.execute("SELECT tadoku_user_id FROM users WHERE discord_id=?",
                                   (att["discord_id"],)).fetchone()
            if not binding or binding["tadoku_user_id"] != evidence.get("tadoku_user_id"):
                return {"ok": False, "error": "Tadoku link changed; confirm again.", "attempt": att}


        challenge = conn.execute("SELECT * FROM challenge_defs WHERE id=?",
                                 (att["challenge_id"],)).fetchone()
        snapshot = {**dict(challenge or {}), **att}
        cur = conn.execute(
            "UPDATE attempts SET status='completed', completed_at=?, "
            "verify_method=?, evidence_json=?, note=?, content=?, challenge_snapshot_json=? "
            "WHERE id=? AND status='active'",
            (
                now,
                method,
                json.dumps(evidence),
                note,
                content_val,
                completion_snapshot(snapshot),
                att["id"],
            ),
        )
        if not cur.rowcount:
            return {"ok": False, "error": "This attempt is no longer active."}
        completion_facts = {
            **snapshot, 'status': 'completed', 'completed_at': now,
            'confirmed_at': now, 'verified_at': now,
            'verify_method': method, 'evidence_json': json.dumps(evidence),
            'note': note, 'content': content_val,
            'edition_year': att.get('edition_year') or (dict(challenge or {}).get('year')),
            'completion_time_quality': 'confirmation_proxy',
        }
        conn.execute('UPDATE attempts SET confirmed_at=?, verified_at=?, original_completion_json=COALESCE(original_completion_json,?), effective_completion_json=? WHERE id=?',
                     (now, now, json.dumps(completion_facts), json.dumps(completion_facts), att['id']))
        _record_transition(conn, att, 'completed', now)
        from app.services import scoring, badges
        scoring.ensure_completion_award(conn, att['id'])
        badges.sync_user(conn, str(att['discord_id']))
        sync_user(conn, str(att["discord_id"]))
    att_out = {**att, "content": content_val, "status": "completed", "completed_at": now}
    return {
        "ok": True,
        "attempt": att_out,
        "completed_at": now,
        "method": method,
        "content": content_val,
        "verify": _result_dict(verify_result) if verify_result else None,
    }


def admin_update_attempt(
    attempt_id: int,
    *,
    status: str | None = None,
    param: str | None = None,
    content: str | None = None,
    note: str | None = None,
    ends_at: str | None = None,
    started_at: str | None = None,
    verify_method: str | None = None,
    completed_at: str | None = None,
) -> dict[str, Any]:
    fields = []
    args: list[Any] = []
    if status is not None:
        if status not in ATTEMPT_STATUSES:
            return {"ok": False, "error": "Invalid status"}
        fields.append("status=?")
        args.append(status)
        if status == "completed":
            # Editing a completed attempt must not change its completion date.
            fields.append("completed_at=CASE WHEN status='completed' THEN completed_at "
                          "ELSE COALESCE(completed_at,?) END")
            args.append(utcnow_iso())
    if completed_at is not None:
        try:
            date = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            date = date.replace(tzinfo=date.tzinfo or timezone.utc).astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            return {"ok": False, "error": "Enter a valid completion date and time."}
        fields.append("completed_at=?")
        args.append(date.replace(microsecond=0).isoformat())
    if param is not None:
        fields.append("param=?")
        args.append(param or None)
    if content is not None:
        fields.append("content=?")
        args.append(content or None)
    if note is not None:
        fields.append("note=?")
        args.append(note)
    if ends_at is not None:
        fields.append("ends_at=?")
        args.append(ends_at)
    if started_at is not None:
        fields.append("started_at=?")
        args.append(started_at)
    if verify_method is not None:
        fields.append("verify_method=?")
        args.append(verify_method)
    if not fields:
        return {"ok": False, "error": "No changes"}
    try:
        aid = int(attempt_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Attempt not found"}
    if aid < 1 or aid > SQL_INT_MAX:
        return {"ok": False, "error": "Attempt not found"}
    args.append(aid)
    with db_session() as conn:
        from app.services.achievements import completion_snapshot, sync_user

        previous = conn.execute("SELECT * FROM attempts WHERE id=?", (aid,)).fetchone()
        if previous is None:
            return {"ok": False, "error": "Attempt not found"}
        if completed_at is not None:
            if (status or previous["status"]) != "completed":
                return {"ok": False, "error": "Only completed attempts can have a completion date."}
            start = parse_iso(started_at or previous["started_at"])
            start = start.replace(tzinfo=start.tzinfo or timezone.utc)
            if date < start:
                return {"ok": False, "error": "The completion date cannot precede the start date."}
        cur = conn.execute(
            f"UPDATE attempts SET {', '.join(fields)} WHERE id=?", args
        )
        if cur.rowcount == 0:
            return {"ok": False, "error": "Attempt not found"}
        current = conn.execute("SELECT * FROM attempts WHERE id=?", (aid,)).fetchone()
        if current["status"] == "completed" and not current["challenge_snapshot_json"]:
            challenge = conn.execute("SELECT * FROM challenge_defs WHERE id=?",
                                     (current["challenge_id"],)).fetchone()
            if challenge:
                conn.execute("UPDATE attempts SET challenge_snapshot_json=? WHERE id=?",
                             (completion_snapshot(dict(challenge)), aid))
        import uuid
        operation_id = 'legacy-admin:' + str(uuid.uuid4())
        conn.execute('UPDATE attempts SET correction_revision=COALESCE(correction_revision,0)+1 WHERE id=?', (aid,))
        conn.execute('INSERT INTO attempt_events(attempt_id,event_type,before_json,after_json,effective_at,recorded_at,actor,source,reason,operation_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (aid, 'legacy_admin_correction', json.dumps(dict(previous)), json.dumps(dict(current)), utcnow_iso(), utcnow_iso(), 'legacy_admin', 'legacy_admin', 'Compatibility admin update; use preview/apply correction for intentional timing exceptions', operation_id))
        from app.services import scoring, badges
        scoring.sync_user(conn, str(current['discord_id']))
        badges.sync_user(conn, str(current['discord_id']))
        sync_user(conn, str(current["discord_id"]))
    return {"ok": True}


def admin_upsert_challenge(data: dict[str, Any], *, conn=None, publish=True) -> dict[str, Any]:
    """Compatibility writer; reviewed services supply their own transaction.

    New human administration should use catalog_admin preview/apply. Explicit
    actor/source/reason is retained on new immutable definition revisions.
    """
    required = ["id", "year", "tier", "number", "title", "goal_kind", "goal_amount"]
    for k in required:
        if k not in data or data[k] in (None, ""):
            return {"ok": False, "error": f"Missing {k}"}
    param = data.get("param_label")
    if param in ("", "null"):
        param = None
    concurrent = 1 if challenge_allows_concurrent(
        {"allows_concurrent": data.get("allows_concurrent")}
    ) else 0
    fixed = data.get("fixed_param")
    if fixed in ("", "null", None):
        fixed = None
    else:
        fixed = str(fixed).strip() or None
    raw_pts = data.get("points")
    points = (
        None
        if raw_pts in (None, "", "null")
        else sql_int(raw_pts, lo=0, hi=COUNTER_MAX)
    )
    with (nullcontext(conn) if conn is not None else db_session()) as conn:
        previous_revision = conn.execute('SELECT COALESCE(MAX(id),0) FROM definition_revisions').fetchone()[0]
        existing = conn.execute(
            "SELECT * FROM challenge_defs WHERE id=?", (data["id"],)
        ).fetchone()
        if existing and data.get('identity_id') and data['identity_id'] != existing['identity_id']:
            return {'ok': False, 'error': 'A published edition cannot change permanent identity'}
        if existing and int(data['year']) != int(existing['year']):
            return {'ok': False, 'error': 'Publish a new edition ID rather than changing its catalog year.'}
        group = str(data.get('duration_group') or (existing['duration_group'] if existing else ('special' if data['tier'] == 'special' else 'daily'))).lower()
        repeat = str(data.get('repeat_policy') or (existing['repeat_policy'] if existing else 'legacy')).lower()
        if group not in ('daily', 'monthly', 'yearly', 'special') or repeat not in ('legacy', 'annual', 'once_ever'):
            return {'ok': False, 'error': 'Invalid duration group or repeat policy'}
        from app.services.lifecycle import calculate_deadline, ensure_catalog_records
        try:
            calculate_deadline({**data, 'duration_group': group}, datetime.now(timezone.utc))
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}
        category = str(
            data.get("category") or (existing["category"] if existing else "ota")
        ).strip().lower()
        if category not in ("ota", "physical"):
            return {"ok": False, "error": "Invalid category"}
        conn.execute(
            """
            INSERT INTO challenge_defs(
              id, year, tier, number, title, description, time_limit_hours,
              param_label, verify, goal_kind, goal_amount, media_filter,
              allows_concurrent, fixed_param, points, category
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              year=excluded.year, tier=excluded.tier, number=excluded.number,
              title=excluded.title, description=excluded.description,
              time_limit_hours=excluded.time_limit_hours,
              param_label=excluded.param_label, verify=excluded.verify,
              goal_kind=excluded.goal_kind, goal_amount=excluded.goal_amount,
              media_filter=excluded.media_filter,
              allows_concurrent=excluded.allows_concurrent,
              fixed_param=excluded.fixed_param,
              points=excluded.points,
              category=excluded.category
            """,
            (
                data["id"],
                sql_int(data["year"], lo=1, hi=COUNTER_MAX),
                data["tier"],
                sql_int(data["number"], lo=1, hi=COUNTER_MAX),
                data["title"],
                data.get("description") or "",
                _bound_hours(data.get("time_limit_hours")),
                param,
                data.get("verify") or "tadoku",
                data["goal_kind"],
                min(max(float(data["goal_amount"] or 0), 0.0), 1e12),
                data.get("media_filter") or "any",
                concurrent,
                fixed,
                points,
                category,
            ),
        )
        conn.execute('UPDATE challenge_defs SET identity_id=?, edition_id=?, duration_group=?, repeat_policy=? WHERE id=?', (
            existing['identity_id'] if existing else data.get('identity_id') or data['id'],
            existing['edition_id'] if existing else data.get('edition_id') or data['id'], group, repeat, data['id']))
        for field in ('available_from', 'available_until', 'published_at'):
            if field in data:
                conn.execute(f'UPDATE challenge_defs SET {field}=? WHERE id=?', (data[field] or None, data['id']))
        ensure_catalog_records(conn)
        from app.services.badges import publish_rosters
        if publish:
            publish_rosters(conn)
        updated = conn.execute(
            "SELECT * FROM challenge_defs WHERE id=?", (data["id"],)
        ).fetchone()
        actor = str(data.get('actor') or 'legacy_admin')
        reason = str(data.get('reason') or 'Compatibility catalog update')
        source = str(data.get('source') or 'legacy_admin')
        conn.execute('UPDATE definition_revisions SET actor=?,source=?,reason=? WHERE id>? AND challenge_id=?',
                     (actor, source, reason, previous_revision, data['id']))
        record_change(conn, existing, updated, note=reason)
    return {"ok": True}


def admin_delete_challenge(challenge_id: str) -> dict[str, Any]:
    from app.services.lifecycle import retire_challenge
    return retire_challenge(challenge_id, actor='legacy_admin', reason='Administrative retirement (legacy delete action)')


def completed_challenge_ids(discord_id: str) -> set[str]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT DISTINCT challenge_id FROM attempts "
            "WHERE discord_id=? AND status='completed'",
            (str(discord_id),),
        ).fetchall()
    return {str(r["challenge_id"]) for r in rows}


def reattempts_allowed() -> bool:
    return bool(_runtime().get("allow_reattempt"))


def completion_counts(discord_id: str) -> dict[str, int]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT c.tier, COUNT(*) AS n FROM attempts a "
            "JOIN challenge_defs c ON c.id=a.challenge_id "
            "WHERE a.discord_id=? AND a.status='completed' GROUP BY c.tier",
            (discord_id,),
        ).fetchall()
        by_tier = {r["tier"]: r["n"] for r in rows}
        total = sum(by_tier.values())
        by_tier["total"] = total
        return by_tier


def _fail_sql(alias: str = "") -> str:
    """Timeout, or quit after the window was live (scheduled cancel is not a loss)."""
    p = f"{alias}." if alias else ""
    return (
        f"({p}status='expired' OR "
        f"({p}status='abandoned' AND {p}verify_method='quit'))"
    )


def failure_count(discord_id: str) -> int:
    """Lifetime losses: expired, or abandoned while active."""
    with db_session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM attempts "
            f"WHERE discord_id=? AND {_fail_sql()}",
            (str(discord_id),),
        ).fetchone()
        return sql_int(row["n"] if row else 0)


def total_completions() -> int:
    with db_session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM attempts WHERE status='completed'"
        ).fetchone()
        return int(row["n"] if row else 0)


# Prestige ladder only (Special is a side board — mixed points, no sweep/best-tier).
_TIER_RANK = {
    "easy": 1,
    "medium": 2,
    "hard": 3,
    "ota_master": 4,
}
_TIER_POINTS_SQL = (
    "COALESCE(c.points, CASE LOWER(c.tier) "
    "WHEN 'easy' THEN 1 WHEN 'medium' THEN 2 WHEN 'hard' THEN 3 "
    "WHEN 'ota_master' THEN 4 ELSE 0 END)"
)


def _sweep_bonus_total(swept: list[str]) -> int:
    return sum(sweep_bonus(t) for t in swept)


def _scored_board() -> list[dict[str, Any]]:
    """All completers with sweep bonus, sorted points / clears / fewer fails."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT a.discord_id, u.display_name, "
            "COUNT(*) AS clears, "
            f"SUM({_TIER_POINTS_SQL}) AS points, "
            "COALESCE(("
            "  SELECT COUNT(*) FROM attempts f "
            f"  WHERE f.discord_id = a.discord_id AND {_fail_sql('f')}"
            "), 0) AS fails "
            "FROM attempts a "
            "JOIN challenge_defs c ON c.id=a.challenge_id "
            "LEFT JOIN users u ON u.discord_id=a.discord_id "
            "WHERE a.status='completed' GROUP BY a.discord_id"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["fails"] = sql_int(d.get("fails") or 0)
        d["clears"] = sql_int(d.get("clears") or 0)
        d["points"] = sql_int(d.get("points") or 0)
        out.append(d)
    if out:
        with db_session() as conn:
            from app.services import scoring, badges
            for d in out:
                scoring.sync_user(conn, str(d['discord_id']))
                badges.sync_user(conn, str(d['discord_id']))
                d['points'] = scoring.user_points(conn, str(d['discord_id']))
                d['n'] = d['points']
        out.sort(
            key=lambda d: (-int(d["points"]), -int(d["clears"]), int(d["fails"]))
        )
    return out


def top_completers(limit: int = 15) -> list[dict[str, Any]]:
    """Leaders by points, then clears, then fewer fails.

    Keys: discord_id, display_name, clears, points, fails, n (=points, compat).
    """
    return _scored_board()[: sql_int(limit, lo=1, hi=100)]


def challenge_points(discord_id: str) -> int:
    """Tier-weighted leaderboard points (clears + sweep bonuses)."""
    with db_session() as conn:
        from app.services import scoring, badges
        scoring.sync_user(conn, str(discord_id))
        badges.sync_user(conn, str(discord_id))
        return scoring.user_points(conn, str(discord_id))


def leaderboard_standing(discord_id: str) -> dict[str, Any]:
    """points, rank (1-based or None if 0 pts), ranked (players with points)."""
    did = str(discord_id)
    rows = _scored_board()
    ranked = sum(1 for r in rows if int(r["points"]) > 0)
    points = next(
        (int(r["points"]) for r in rows if str(r["discord_id"]) == did),
        0,
    )
    if points <= 0:
        return {"points": points, "rank": None, "ranked": ranked}
    better = sum(1 for r in rows if int(r["points"]) > points)
    return {"points": points, "rank": better + 1, "ranked": ranked}


def completed_tiers(discord_id: str, year: int | None = None) -> list[str]:
    """Tiers where this user has cleared every catalog challenge for `year`."""
    return completed_tiers_for([discord_id], year=year).get(str(discord_id), [])


def completed_tiers_for(
    discord_ids: list[str], year: int | None = None
) -> dict[str, list[str]]:
    """Daily-only styling, backed by earned year-scoped receipts."""
    ids = [str(i) for i in discord_ids if i]
    out: dict[str, list[str]] = {str(i): [] for i in discord_ids}
    if not ids:
        return out
    with db_session() as conn:
        from app.services import badges
        if year is None:
            # Name styling and compact Daily stamps roll over with the header,
            # even when next year's catalog has not yet been published.
            year = datetime.now(timezone.utc).year
        for did in ids:
            badges.sync_user(conn, did)
            tiers = {r['tier'] for r in conn.execute("SELECT tier FROM badge_receipts WHERE discord_id=? AND duration_group='daily' AND year=? AND status='active'", (did, int(year)))}
            out[did] = [tier for tier in _TIER_RANK if tier in tiers]
    return out


def tier_slot_progress(
    discord_id: str, year: int | None = None
) -> dict[str, list[bool]]:
    """Prestige catalog slots in number order; True if that challenge is cleared."""
    out: dict[str, list[bool]] = {t: [] for t in _TIER_RANK}
    did = str(discord_id)
    with db_session() as conn:
        if year is None:
            row = conn.execute(
                "SELECT MAX(year) AS y FROM challenge_defs"
            ).fetchone()
            year = int(row["y"]) if row and row["y"] is not None else None
        if year is None:
            return out
        from app.catalog import availability_sql
        predicate, times = availability_sql()
        catalog = conn.execute(
            "SELECT id, LOWER(tier) AS tier FROM challenge_defs "
            "WHERE year=? AND duration_group='daily' AND " + predicate + ' ORDER BY number, id',
            (int(year), *times),
        ).fetchall()
        done = {
            str(r["challenge_id"])
            for r in conn.execute(
                "SELECT DISTINCT challenge_id FROM attempts "
                "WHERE discord_id=? AND status='completed'",
                (did,),
            ).fetchall()
        }
    for r in catalog:
        t = str(r["tier"])
        if t in out:
            out[t].append(str(r["id"]) in done)
    return out


def category_progress(discord_id: str, year: int | None = None) -> dict[str, dict]:
    """Current catalog points; repeats and leaderboard sweep bonuses are excluded."""
    out = {
        key: {"points": 0, "total_points": 0, "clears": 0, "total": 0,
              "breakdown": {"Reading": 0, "Listening": 0, "Normal": 0}}
        for key in ("ota", "physical")
    }
    with db_session() as conn:
        if year is None:
            row = conn.execute("SELECT MAX(year) AS y FROM challenge_defs").fetchone()
            year = row["y"] if row else None
        if year is None:
            return out
        from app.services import scoring
        scoring.sync_user(conn, str(discord_id))
        from app.catalog import availability_sql
        predicate, times = availability_sql('c')
        rows = conn.execute(
            "SELECT c.*, l.effective_points AS earned_points, a.rules_snapshot_json AS earned_rules, "
            "a.effective_completion_json AS earned_facts FROM challenge_defs c "
            "LEFT JOIN attempts a ON a.id=(SELECT first.id FROM attempts first WHERE first.challenge_id=c.id "
            "AND first.discord_id=? AND first.status='completed' ORDER BY first.id LIMIT 1) "
            "LEFT JOIN award_ledger l ON l.attempt_id=a.id AND l.award_type='completion' "
            "WHERE c.year=? AND " + predicate,
            (str(discord_id), int(year), *times),
        ).fetchall()
    for row in rows:
        c = dict(row)
        if c['earned_points'] is not None:
            accepted = {**json.loads(c.get('earned_rules') or '{}'), **json.loads(c.get('earned_facts') or '{}')}
            c.update({key: accepted[key] for key in ('category', 'media_filter') if accepted.get(key) is not None})
        stats = out[c["category"]]
        points = int(c['earned_points']) if c['earned_points'] is not None else reward_points(c)
        stats["total"] += 1
        stats["total_points"] += points
        if c['earned_points'] is None:
            continue
        stats["clears"] += 1
        stats["points"] += points
        media = str(c.get("media_filter") or "any").lower().strip()
        kind = (
            "Reading" if media in ("reading", "manga", "comic", "book", "novel",
                                   "ln", "light_novel", "vn", "visual_novel", "visual novel")
            else "Listening" if media in ("listening", "anime", "shows", "show",
                                         "tv", "movie", "youtube", "video", "audiobook")
            else "Normal"
        )
        stats["breakdown"][kind] += points
    return out


def highest_completed_tier(discord_id: str) -> str | None:
    """Highest fully swept prestige tier, or None."""
    m = highest_completed_tiers([discord_id])
    return m.get(discord_id)


def highest_completed_tiers(discord_ids: list[str]) -> dict[str, str | None]:
    """Map discord_id → highest fully swept prestige tier."""
    swept = completed_tiers_for(discord_ids)
    return {did: (tiers[-1] if tiers else None) for did, tiers in swept.items()}
