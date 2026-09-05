from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.db import db_session


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
        if replace_year:
            conn.execute("DELETE FROM challenge_defs WHERE year = ?", (year,))
        n = 0
        for c in items:
            param = c.get("param_label")
            if param in (None, "null", ""):
                param = None
            conn.execute(
                """
                INSERT INTO challenge_defs(
                  id, year, tier, number, title, description,
                  time_limit_hours, param_label, verify,
                  goal_kind, goal_amount, media_filter
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
                  media_filter=excluded.media_filter
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
                ),
            )
            n += 1
        return n


def list_challenges(year: int | None = None, tier: str | None = None) -> list[dict]:
    q = "SELECT * FROM challenge_defs WHERE 1=1"
    args: list[Any] = []
    if year is not None:
        q += " AND year = ?"
        args.append(year)
    if tier:
        q += " AND tier = ?"
        args.append(tier)
    q += " ORDER BY year DESC, CASE tier "
    q += "WHEN 'easy' THEN 1 WHEN 'medium' THEN 2 WHEN 'hard' THEN 3 "
    q += "WHEN 'ota_master' THEN 4 ELSE 9 END, number"
    with db_session() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def get_challenge(challenge_id: str) -> dict | None:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM challenge_defs WHERE id = ?", (challenge_id,)
        ).fetchone()
        return dict(row) if row else None


def get_challenge_by_tier_num(tier: str, number: int, year: int = 2026) -> dict | None:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM challenge_defs WHERE year=? AND tier=? AND number=?",
            (year, tier, number),
        ).fetchone()
        return dict(row) if row else None
