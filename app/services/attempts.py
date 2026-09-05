from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.catalog import get_challenge_by_tier_num
from app.config import get_settings, merge_runtime
from app.db import db_session, get_all_settings, utcnow_iso
from app.services.verify import (
    VerifyResult,
    evaluate_logs,
    parse_iso,
    synthetic_logs_for_goal,
)
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


def active_attempt(discord_id: str) -> dict | None:
    expire_overdue()
    with db_session() as conn:
        row = conn.execute(
            "SELECT a.*, c.title, c.tier, c.number, c.verify, c.goal_kind, "
            "c.goal_amount, c.media_filter, c.param_label "
            "FROM attempts a JOIN challenge_defs c ON c.id = a.challenge_id "
            "WHERE a.discord_id=? AND a.status='active' "
            "ORDER BY a.id DESC LIMIT 1",
            (discord_id,),
        ).fetchone()
        return dict(row) if row else None


def list_attempts(
    discord_id: str | None = None,
    status: str | None = None,
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
    if status:
        q += " AND a.status=?"
        args.append(status)
    q += " ORDER BY a.id DESC LIMIT ?"
    args.append(limit)
    with db_session() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def expire_overdue() -> int:
    now = utcnow_iso()
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE attempts SET status='expired' "
            "WHERE status='active' AND ends_at < ?",
            (now,),
        )
        return cur.rowcount


def start_attempt(
    discord_id: str,
    *,
    tier: str,
    number: int,
    param: str | None,
    guild_id: str | None,
    display_name: str | None = None,
    year: int = 2026,
) -> dict[str, Any]:
    ensure_user(discord_id, display_name)
    expire_overdue()
    rt = _runtime()
    ch = get_challenge_by_tier_num(tier, number, year=year)
    if not ch:
        return {"ok": False, "error": f"No challenge {tier} #{number} for {year}."}

    if ch.get("param_label") and not (param and param.strip()):
        return {
            "ok": False,
            "error": f"This challenge requires **{ch['param_label']}**.",
        }

    existing = active_attempt(discord_id)
    if existing:
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
    hours = float(ch.get("time_limit_hours") or 24)
    ends = now + timedelta(hours=hours)
    with db_session() as conn:
        cur = conn.execute(
            "INSERT INTO attempts("
            "discord_id, challenge_id, param, status, started_at, ends_at, guild_id"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                discord_id,
                ch["id"],
                (param or "").strip() or None,
                "active",
                now.isoformat(),
                ends.isoformat(),
                guild_id,
            ),
        )
        attempt_id = cur.lastrowid

    return {
        "ok": True,
        "attempt_id": attempt_id,
        "challenge": ch,
        "started_at": now.isoformat(),
        "ends_at": ends.isoformat(),
        "param": (param or "").strip() or None,
    }


def abandon_attempt(discord_id: str) -> dict[str, Any]:
    att = active_attempt(discord_id)
    if not att:
        return {"ok": False, "error": "No active challenge."}
    with db_session() as conn:
        conn.execute(
            "UPDATE attempts SET status='abandoned' WHERE id=? AND status='active'",
            (att["id"],),
        )
    return {"ok": True, "attempt": att}


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
    expire_overdue()
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
        },
        note=note,
        verify_result=result,
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
) -> dict[str, Any]:
    now = utcnow_iso()
    with db_session() as conn:
        conn.execute(
            "UPDATE attempts SET status='completed', completed_at=?, "
            "verify_method=?, evidence_json=?, note=? WHERE id=? AND status='active'",
            (
                now,
                method,
                json.dumps(evidence),
                note,
                att["id"],
            ),
        )
    return {
        "ok": True,
        "attempt": att,
        "completed_at": now,
        "method": method,
        "verify": _result_dict(verify_result) if verify_result else None,
    }


def admin_update_attempt(
    attempt_id: int,
    *,
    status: str | None = None,
    param: str | None = None,
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
