from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.db import db_session, utcnow_iso
from app.catalog_changes import record_import, snapshot


def _as_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return False


def load_yaml(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("catalog must be a mapping")
    return data


def upsert_challenges_from_yaml(path: str | Path, *, replace_year: bool = False, seed_only: bool = False) -> int:
    data = load_yaml(path)
    year = int(data["year"])
    items = data.get("challenges") or []
    with db_session() as conn:
        before = snapshot(conn)
        conn.execute("BEGIN IMMEDIATE")
        incoming_ids = {str(c['id']) for c in items}
        if len(incoming_ids) != len(items):
            raise ValueError("Duplicate catalog IDs")
        if replace_year:
            # Omission is retirement, never permission to erase published history.
            for old in conn.execute("SELECT id FROM challenge_defs WHERE year=? AND tier!='special'", (year,)):
                if old['id'] not in incoming_ids:
                    conn.execute("UPDATE challenge_defs SET lifecycle_state='retired', retired_at=COALESCE(retired_at,?) WHERE id=?", (utcnow_iso(), old['id']))
        n = 0
        for c in items:
            existing = conn.execute(
                "SELECT * FROM challenge_defs WHERE id=?", (c["id"],)
            ).fetchone()
            if existing and seed_only:
                continue
            if existing and str(existing["tier"] or "").lower() == "special":
                incoming = str(c.get("tier") or "").lower()
                if incoming != "special" or int(existing["year"]) != year:
                    continue
            if existing and int(existing['year']) != year:
                raise ValueError("An edition ID cannot be moved to a different year; publish a new ID with an explicit identity_id")
            from app.services.lifecycle import calculate_deadline
            from datetime import datetime, timezone
            group = str(c.get('duration_group') or (existing['duration_group'] if existing else ('special' if c.get('tier') == 'special' else 'daily'))).lower()
            repeat = str(c.get('repeat_policy') or (existing['repeat_policy'] if existing else 'legacy')).lower()
            if group not in ('daily', 'weekly', 'monthly', 'yearly', 'special') or repeat not in ('legacy', 'annual', 'once_ever'):
                raise ValueError('Invalid duration group or repeat policy')
            calculate_deadline({**c, 'duration_group': group}, datetime.now(timezone.utc))
            if existing and c.get('identity_id') and c['identity_id'] != existing['identity_id']:
                raise ValueError('A published edition cannot change permanent identity')
            param = c.get("param_label")
            if param in (None, "null", ""):
                param = None
            concurrent = 1 if _as_bool(c.get("allows_concurrent")) else 0
            fixed = c.get("fixed_param")
            if fixed in (None, "null", ""):
                fixed = None
            else:
                fixed = str(fixed).strip() or None
            raw_pts = c.get("points")
            points = None if raw_pts in (None, "", "null") else int(raw_pts)
            category = str(c.get("category") or "ota").strip().lower()
            if category not in ("ota", "physical"):
                raise ValueError(f"Invalid category: {category}")
            conn.execute(
                """
                INSERT INTO challenge_defs(
                  id, year, tier, number, title, description,
                  time_limit_hours, param_label, verify,
                  goal_kind, goal_amount, media_filter, allows_concurrent,
                  fixed_param, points, category
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  year=excluded.year,
                  tier=excluded.tier,
                  number=excluded.number,
                  title=excluded.title,
                  description=excluded.description,
                  time_limit_hours=excluded.time_limit_hours,
                  param_label=excluded.param_label,
                  verify=excluded.verify,
                  goal_kind=excluded.goal_kind,
                  goal_amount=excluded.goal_amount,
                  media_filter=excluded.media_filter,
                  allows_concurrent=excluded.allows_concurrent,
                  fixed_param=excluded.fixed_param,
                  points=excluded.points,
                  category=excluded.category
                """,
                (
                    c["id"],
                    year,
                    c["tier"],
                    int(c["number"]),
                    c["title"],
                    c.get("description") or "",
                    float(c.get("time_limit_hours") or 24),
                    param,
                    c.get("verify") or "tadoku",
                    c["goal_kind"],
                    float(c["goal_amount"]),
                    c.get("media_filter") or "any",
                    concurrent,
                    fixed,
                    points,
                    category,
                ),
            )
            # Missing lifecycle fields preserve admin edits, especially retirement.
            state = c.get('lifecycle_state') or (existing['lifecycle_state'] if existing else 'active')
            if state not in ('active', 'retired'):
                raise ValueError('Invalid lifecycle state')
            conn.execute("UPDATE challenge_defs SET identity_id=?, edition_id=?, duration_group=?, repeat_policy=?, lifecycle_state=?, available_from=?, available_until=?, published_at=? WHERE id=?", (
                c.get('identity_id') or (existing['identity_id'] if existing else c['id']),
                existing['edition_id'] if existing else c.get('edition_id') or c['id'],
                group, repeat, state,
                c.get('available_from', existing['available_from'] if existing else None),
                c.get('available_until', existing['available_until'] if existing else None),
                c.get('published_at', existing['published_at'] if existing else None), c['id']))
            n += 1
        from app.services.lifecycle import ensure_catalog_records
        ensure_catalog_records(conn)
        from app.services.badges import publish_rosters
        publish_rosters(conn)
        record_import(conn, before)
        return n


def availability_sql(alias='challenge_defs', *, at=None):
    """Shared effective-time predicate for catalog and progress projections.

    Alias is an internal SQL identifier; all timestamp values remain bound data.
    One captured instant is reused across every predicate in a query.
    """
    if alias not in ('challenge_defs', 'c'):
        raise ValueError('Unsupported catalog SQL alias')
    from datetime import datetime, timezone
    from app.services.lifecycle import _date
    instant = _date(at).isoformat() if at is not None else datetime.now(timezone.utc).isoformat()
    a = alias
    sql = f"""({a}.lifecycle_state='active' OR ({a}.lifecycle_state='retired' AND julianday({a}.retired_at)>julianday(?)) OR julianday({a}.restore_at)<=julianday(?))
      AND ({a}.retired_at IS NULL OR julianday({a}.retired_at)>julianday(?) OR julianday({a}.restore_at)<=julianday(?))
      AND ({a}.published_at IS NULL OR julianday({a}.published_at)<=julianday(?))
      AND ({a}.available_from IS NULL OR julianday({a}.available_from)<=julianday(?))
      AND ({a}.available_until IS NULL OR julianday({a}.available_until)>julianday(?))
      AND NOT EXISTS(SELECT 1 FROM challenge_identities i WHERE i.id={a}.identity_id
        AND i.lifecycle_state='retired' AND (i.retired_at IS NULL OR julianday(i.retired_at)<=julianday(?))
        AND (i.restore_at IS NULL OR julianday(i.restore_at)>julianday(?)))"""
    return sql, [instant] * sql.count('?')


def list_challenges(year: int | None = None, tier: str | None = None, *, duration_group: str | None = None, include_retired: bool = False) -> list[dict]:
    q = "SELECT * FROM challenge_defs WHERE 1=1"
    args: list[Any] = []
    if not include_retired:
        predicate, times = availability_sql()
        q += ' AND ' + predicate
        args.extend(times)
    if duration_group:
        q += " AND duration_group=?"
        args.append(duration_group)
    if year is not None:
        q += " AND (year = ? OR tier = 'special')"
        args.append(year)
    if tier:
        q += " AND tier = ?"
        args.append(tier)
    q += " ORDER BY year DESC, CASE tier "
    q += "WHEN 'easy' THEN 1 WHEN 'medium' THEN 2 WHEN 'hard' THEN 3 "
    q += "WHEN 'ota_master' THEN 4 WHEN 'special' THEN 5 ELSE 9 END, number"
    with db_session() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def challenge_count(year: int | None = None) -> int:
    q = "SELECT COUNT(*) AS n FROM challenge_defs WHERE lifecycle_state='active'"
    args: list[Any] = []
    if year is not None:
        q += " AND (year = ? OR tier = 'special')"
        args.append(int(year))
    with db_session() as conn:
        row = conn.execute(q, args).fetchone()
        return int(row["n"] if row else 0)


def get_challenge(challenge_id: str) -> dict | None:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM challenge_defs WHERE id = ?", (challenge_id,)
        ).fetchone()
        return dict(row) if row else None


def needs_focus_param(ch: dict | None) -> bool:
    """Ask for a named work only when the goal is one specific title."""
    if not ch:
        return False
    if not str(ch.get("param_label") or "").strip():
        return False
    if str(ch.get("fixed_param") or "").strip():
        return False
    return str(ch.get("goal_kind") or "").lower() == "complete_work"


_TIER_POINTS = {"easy": 1, "medium": 2, "hard": 3, "ota_master": 4}
SWEEP_BONUS_MULT = 2


def sweep_bonus(tier: str) -> int:
    """Bonus for 100% clearing a prestige rank: 2 × scale (easy = 2)."""
    return SWEEP_BONUS_MULT * _TIER_POINTS.get(str(tier or "").lower(), 0)


def reward_points(ch: dict | None) -> int:
    if not ch:
        return 0
    from app.services import scoring
    with db_session() as conn:
        return scoring.reward_points(conn, ch)


def special_points_note(ch: dict | None) -> str:
    """'1 pt' / '2 pts' for Special listings; empty on ladder tiers."""
    if not ch or str(ch.get("tier") or "").lower() != "special":
        return ""
    n = reward_points(ch)
    return f"{n} pt" if n == 1 else f"{n} pts"


def special_points_suffix(ch: dict | None) -> str:
    note = special_points_note(ch)
    return f" · **{note}**" if note else ""


def get_challenge_by_tier_num(tier: str, number: int, year: int = 2026) -> dict | None:
    with db_session() as conn:
        if str(tier or "").lower() == "special":
            row = conn.execute(
                "SELECT * FROM challenge_defs WHERE tier='special' AND number=? "
                "ORDER BY year ASC, id LIMIT 1",
                (int(number),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM challenge_defs WHERE year=? AND tier=? AND number=?",
                (year, tier, number),
            ).fetchone()
        return dict(row) if row else None
