from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.catalog import get_challenge, get_challenge_by_tier_num
from app.config import get_settings, merge_runtime
from app.db import db_session, get_all_settings, utcnow_iso
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


def display_content(attempt: dict | None) -> str:
    """Content shown on completed/recent lists (stored content, else param)."""
    if not attempt:
        return ""
    return (attempt.get("content") or attempt.get("param") or "").strip()
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


def _attempt_join_row(discord_id: str, statuses: tuple[str, ...]) -> dict | None:
    placeholders = ",".join("?" * len(statuses))
    with db_session() as conn:
        row = conn.execute(
            "SELECT a.*, c.title, c.tier, c.number, c.verify, c.goal_kind, "
            "c.goal_amount, c.media_filter, c.param_label "
            "FROM attempts a JOIN challenge_defs c ON c.id = a.challenge_id "
            f"WHERE a.discord_id=? AND a.status IN ({placeholders}) "
            "ORDER BY a.id DESC LIMIT 1",
            (discord_id, *statuses),
        ).fetchone()
        return dict(row) if row else None


def active_attempt(discord_id: str) -> dict | None:
    activate_due_scheduled()
    expire_overdue()
    return _attempt_join_row(discord_id, ("active",))


def scheduled_attempt(discord_id: str) -> dict | None:
    activate_due_scheduled()
    return _attempt_join_row(discord_id, ("scheduled",))


def open_attempt(discord_id: str) -> dict | None:
    """Active or scheduled (blocks starting another)."""
    activate_due_scheduled()
    expire_overdue()
    return _attempt_join_row(discord_id, ("active", "scheduled"))


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
        return dict(row) if row else None


def list_attempts(
    discord_id: str | None = None,
    status: str | None = None,
    statuses: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    q = (
        "SELECT a.*, c.title, c.tier, c.number, u.display_name, u.tadoku_user_id "
        "FROM attempts a "
        "JOIN challenge_defs c ON c.id = a.challenge_id "
        "LEFT JOIN users u ON u.discord_id = a.discord_id WHERE 1=1"
    )
    args: list[Any] = []
    if discord_id:
        q += " AND a.discord_id=?"
        args.append(discord_id)
    if statuses:
        ph = ",".join("?" * len(statuses))
        q += f" AND a.status IN ({ph})"
        args.extend(statuses)
    elif status:
        q += " AND a.status=?"
        args.append(status)
    q += " ORDER BY a.id DESC LIMIT ?"
    args.append(limit)
    with db_session() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


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
            "WHERE a.status='active' AND a.ends_at < ?",
            (now,),
        ).fetchall()
        expired = [dict(r) for r in rows]
        if expired:
            conn.execute(
                "UPDATE attempts SET status='expired' "
                "WHERE status='active' AND ends_at < ?",
                (now,),
            )
        return expired


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
                row["status"] = "active"
        return due


def tick_lifecycle() -> tuple[list[dict], list[dict]]:
    """Activate due schedules, then expire overdue actives."""
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

    if ch.get("param_label") and not (param and param.strip()):
        return {
            "ok": False,
            "error": f"This challenge requires **{ch['param_label']}**.",
        }

    existing = open_attempt(discord_id)
    if existing:
        st = existing.get("status")
        if st == "scheduled":
            when = int(parse_iso(existing["started_at"]).timestamp())
            return {
                "ok": False,
                "error": (
                    f"You already scheduled **{existing['title']}** "
                    f"(starts <t:{when}:R>). `/challenge abandon` to cancel."
                ),
            }
        return {
            "ok": False,
            "error": (
                f"You already have an active challenge: **{existing['title']}** "
                f"(ends <t:{int(parse_iso(existing['ends_at']).timestamp())}:R>)."
            ),
        }

    if not rt["allow_reattempt"]:
        with db_session() as conn:
            done = conn.execute(
                "SELECT id FROM attempts WHERE discord_id=? AND challenge_id=? "
                "AND status='completed' LIMIT 1",
                (discord_id, ch["id"]),
            ).fetchone()
        if done:
            return {
                "ok": False,
                "error": "Re-attempts are disabled and you already completed this challenge.",
            }

    if ch.get("verify") == "tadoku":
        user = get_user(discord_id)
        if not user or not user.get("tadoku_user_id"):
            return {
                "ok": False,
                "error": "Link your Tadoku account first: `/challenge link user_id:<uuid>`.",
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

    hours = float(ch.get("time_limit_hours") or 24)
    ends = start_dt + timedelta(hours=hours)
    status = "active" if start_dt <= now else "scheduled"
    with db_session() as conn:
        cur = conn.execute(
            "INSERT INTO attempts("
            "discord_id, challenge_id, param, status, started_at, ends_at, guild_id"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                discord_id,
                ch["id"],
                (param or "").strip() or None,
                status,
                start_dt.isoformat(),
                ends.isoformat(),
                guild_id,
            ),
        )
        attempt_id = cur.lastrowid

    return {
        "ok": True,
        "attempt_id": attempt_id,
        "challenge": ch,
        "status": status,
        "started_at": start_dt.isoformat(),
        "ends_at": ends.isoformat(),
        "param": (param or "").strip() or None,
    }


def abandon_attempt(discord_id: str) -> dict[str, Any]:
    att = open_attempt(discord_id)
    if not att:
        return {"ok": False, "error": "No active or scheduled challenge."}
    with db_session() as conn:
        conn.execute(
            "UPDATE attempts SET status='abandoned' "
            "WHERE id=? AND status IN ('active','scheduled')",
            (att["id"],),
        )
    return {"ok": True, "attempt": {**att, "status": "abandoned"}}


async def confirm_attempt(
    discord_id: str,
    *,
    note: str | None = None,
    force_honor: bool = False,
    dry_run: bool = False,
    fake_logs: list[dict[str, Any]] | None = None,
    method_override: str | None = None,
) -> dict[str, Any]:
    """
    Confirm active attempt. Tadoku challenges must pass verify unless force_honor
    (admin/manual only).

    dry_run: evaluate only, never write completed.
    fake_logs: admin test inject — skip Tadoku fetch, run evaluate_logs on these.
    """
    activate_due_scheduled()
    expire_overdue()
    scheduled = scheduled_attempt(discord_id)
    if scheduled:
        when = int(parse_iso(scheduled["started_at"]).timestamp())
        return {
            "ok": False,
            "error": (
                f"**{scheduled['title']}** is scheduled to start <t:{when}:R>. "
                "Wait until then, or `/challenge abandon` to cancel."
            ),
            "attempt": scheduled,
        }
    att = active_attempt(discord_id)
    if not att:
        return {"ok": False, "error": "No active challenge (or it already expired)."}

    now = datetime.now(timezone.utc).replace(microsecond=0)
    ends = parse_iso(att["ends_at"])
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    if now > ends and not dry_run:
        expire_overdue()
        return {"ok": False, "error": "Challenge window has ended."}

    ch_verify = att.get("verify") or "tadoku"
    rt = _runtime()

    if force_honor and not fake_logs:
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "would_complete": True,
                "method": method_override or "manual",
                "attempt": att,
                "note": note,
                "content": resolve_content(param=att.get("param"), note=note),
            }
        return _complete(
            att,
            method=method_override
            or ("honor" if ch_verify == "honor" else "manual"),
            evidence={"note": note, "forced": force_honor},
            note=note,
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
                "content": resolve_content(param=att.get("param"), note=note),
            }
        return _complete(
            att,
            method="honor",
            evidence={"note": note, "forced": False},
            note=note,
        )

    user = get_user(discord_id)
    logs: list[dict[str, Any]]
    source = "tadoku"

    if fake_logs is not None:
        logs = list(fake_logs)
        source = "fake"
    else:
        if not user or not user.get("tadoku_user_id"):
            return {"ok": False, "error": "Link Tadoku first (`/challenge link`)."}
        started = parse_iso(att["started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        logs = await resolve_logs_for_user(
            tadoku_user_id=user["tadoku_user_id"],
            contest_id=str(rt["tadoku_contest_id"]),
            api_base=str(rt["tadoku_api_base"]),
            since=started,
            until=min(now, ends),
        )

    result = evaluate_logs(
        logs,
        goal_kind=att["goal_kind"],
        goal_amount=float(att["goal_amount"]),
        media_filter=att.get("media_filter") or "any",
        param=att.get("param"),
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
    }
    if not result.ok:
        return {
            "ok": False,
            "error": "Tadoku logs do not meet the goal yet."
            if source == "tadoku"
            else "Fake logs do not meet the goal.",
            "dry_run": dry_run,
            **payload,
        }
    content = resolve_content(
        param=att.get("param"),
        logs=logs,
        matched_ids=result.matched_log_ids,
        note=note,
    )
    payload["content"] = content
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
            "test": source == "fake",
            "content": content,
        },
        note=note,
        verify_result=result,
        logs=logs,
        content=content,
    )


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
    apply=True writes completion (except pure fail cases).
    """
    mode = (mode or "preview_real").lower().strip()
    att = active_attempt(discord_id)
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
    now = utcnow_iso()
    matched = (verify_result.matched_log_ids if verify_result else None) or []
    content_val = content or resolve_content(
        param=att.get("param"),
        logs=logs,
        matched_ids=matched,
        note=note,
    )
    evidence = {**evidence, "content": content_val}
    with db_session() as conn:
        conn.execute(
            "UPDATE attempts SET status='completed', completed_at=?, "
            "verify_method=?, evidence_json=?, note=?, content=? "
            "WHERE id=? AND status='active'",
            (
                now,
                method,
                json.dumps(evidence),
                note,
                content_val,
                att["id"],
            ),
        )
    att_out = {**att, "content": content_val, "status": "completed"}
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
) -> dict[str, Any]:
    fields = []
    args: list[Any] = []
    if status is not None:
        fields.append("status=?")
        args.append(status)
        if status == "completed":
            fields.append("completed_at=?")
            args.append(utcnow_iso())
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
    args.append(attempt_id)
    with db_session() as conn:
        cur = conn.execute(
            f"UPDATE attempts SET {', '.join(fields)} WHERE id=?", args
        )
        if cur.rowcount == 0:
            return {"ok": False, "error": "Attempt not found"}
    return {"ok": True}


def admin_upsert_challenge(data: dict[str, Any]) -> dict[str, Any]:
    required = ["id", "year", "tier", "number", "title", "goal_kind", "goal_amount"]
    for k in required:
        if k not in data or data[k] in (None, ""):
            return {"ok": False, "error": f"Missing {k}"}
    param = data.get("param_label")
    if param in ("", "null"):
        param = None
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO challenge_defs(
              id, year, tier, number, title, description, time_limit_hours,
              param_label, verify, goal_kind, goal_amount, media_filter
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              year=excluded.year, tier=excluded.tier, number=excluded.number,
              title=excluded.title, description=excluded.description,
              time_limit_hours=excluded.time_limit_hours,
              param_label=excluded.param_label, verify=excluded.verify,
              goal_kind=excluded.goal_kind, goal_amount=excluded.goal_amount,
              media_filter=excluded.media_filter
            """,
            (
                data["id"],
                int(data["year"]),
                data["tier"],
                int(data["number"]),
                data["title"],
                data.get("description") or "",
                float(data.get("time_limit_hours") or 24),
                param,
                data.get("verify") or "tadoku",
                data["goal_kind"],
                float(data["goal_amount"]),
                data.get("media_filter") or "any",
            ),
        )
    return {"ok": True}


def admin_delete_challenge(challenge_id: str) -> dict[str, Any]:
    with db_session() as conn:
        conn.execute("DELETE FROM challenge_defs WHERE id=?", (challenge_id,))
    return {"ok": True}


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


def failure_count(discord_id: str) -> int:
    """Lifetime expired attempts (challenge losses on record)."""
    with db_session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM attempts "
            "WHERE discord_id=? AND status='expired'",
            (str(discord_id),),
        ).fetchone()
        return int(row["n"] if row else 0)


def total_completions() -> int:
    with db_session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM attempts WHERE status='completed'"
        ).fetchone()
        return int(row["n"] if row else 0)


def top_completers(limit: int = 15) -> list[dict[str, Any]]:
    """All-time clear leaders: discord_id, display_name, n."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT a.discord_id, u.display_name, COUNT(*) AS n "
            "FROM attempts a LEFT JOIN users u ON u.discord_id=a.discord_id "
            "WHERE a.status='completed' GROUP BY a.discord_id "
            "ORDER BY n DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


# Difficulty ladder for leaderboard row theming
_TIER_RANK = {
    "easy": 1,
    "medium": 2,
    "hard": 3,
    "ota_master": 4,
}
_RANK_TO_TIER = {v: k for k, v in _TIER_RANK.items()}


def highest_completed_tier(discord_id: str) -> str | None:
    """Best tier the user has ever cleared, or None."""
    m = highest_completed_tiers([discord_id])
    return m.get(discord_id)


def highest_completed_tiers(discord_ids: list[str]) -> dict[str, str | None]:
    """Map discord_id → highest completed tier (easy < medium < hard < ota_master)."""
    out: dict[str, str | None] = {str(i): None for i in discord_ids}
    ids = [str(i) for i in discord_ids if i]
    if not ids:
        return out
    ph = ",".join("?" * len(ids))
    with db_session() as conn:
        rows = conn.execute(
            "SELECT a.discord_id AS did, c.tier AS tier "
            "FROM attempts a "
            "JOIN challenge_defs c ON c.id = a.challenge_id "
            f"WHERE a.status='completed' AND a.discord_id IN ({ph})",
            ids,
        ).fetchall()
    best: dict[str, int] = {}
    for r in rows:
        did = str(r["did"])
        rank = _TIER_RANK.get((r["tier"] or "").lower(), 0)
        if rank > best.get(did, 0):
            best[did] = rank
    for did, rank in best.items():
        out[did] = _RANK_TO_TIER.get(rank)
    return out
