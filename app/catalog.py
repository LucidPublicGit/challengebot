from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.db import db_session
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


def upsert_challenges_from_yaml(path: str | Path, *, replace_year: bool = False) -> int:
    data = load_yaml(path)
    year = int(data["year"])
    items = data.get("challenges") or []
    with db_session() as conn:
        before = snapshot(conn)
        if replace_year:
            # ponytail: specials are evergreen — a new year must not wipe them.
            conn.execute(
                "DELETE FROM challenge_defs WHERE year = ? AND tier != 'special'",
                (year,),
            )
        n = 0
        for c in items:
            existing = conn.execute(
                "SELECT year, tier FROM challenge_defs WHERE id=?", (c["id"],)
            ).fetchone()
            if existing and str(existing["tier"] or "").lower() == "special":
                incoming = str(c.get("tier") or "").lower()
                if incoming != "special" or int(existing["year"]) != year:
                    continue
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
            n += 1
        record_import(conn, before)
        return n


def list_challenges(year: int | None = None, tier: str | None = None) -> list[dict]:
    q = "SELECT * FROM challenge_defs WHERE 1=1"
    args: list[Any] = []
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
    q = "SELECT COUNT(*) AS n FROM challenge_defs"
    args: list[Any] = []
    if year is not None:
        q += " WHERE year = ? OR tier = 'special'"
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
    raw = ch.get("points")
    if raw not in (None, ""):
        return int(raw)
    return _TIER_POINTS.get(str(ch.get("tier") or "").lower(), 0)


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
