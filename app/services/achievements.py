"""Editable achievements with immutable IDs and completion-derived unlock dates."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.db import database_path, db_session, utcnow_iso

IMAGE_DIR = Path(__file__).resolve().parents[1] / "images" / "achievements"
FORMATS = {"manga": "manga", "shows": "shows", "youtube": "youtube",
           "vn": "vn", "book": "novel"}
SNAPSHOT_FIELDS = (
    "id", "year", "title", "category", "tier", "number", "media_filter",
    "goal_kind", "goal_amount", "time_limit_hours", "verify",
)


def upload_dir() -> Path:
    # Beside SQLite: persists in the existing Docker data volume across rebuilds.
    return database_path().resolve().parent / "achievement_images"


def completion_snapshot(challenge: dict) -> str:
    snapshot = {key: challenge.get(key) for key in SNAPSHOT_FIELDS}
    snapshot["id"] = challenge.get("challenge_id") or challenge.get("id")
    return json.dumps(snapshot)


def _defaults() -> list[tuple]:
    walk = [f"physical-walk-{t}" for t in ("easy", "medium", "hard", "ota")]
    endurance = [f"physical-endurance-{t}" for t in ("easy", "medium", "hard", "ota")]
    manga = ["easy-1", "medium-1", "hard-2", "ota-2"]
    shows = ["easy-2", "medium-2", "hard-3", "ota-3"]
    reading = manga + ["easy-4", "medium-4", "hard-4", "hard-1", "ota-1"]
    listening = shows + ["easy-3", "medium-3"]
    long_projects = ["medium-8", "medium-7", "medium-5", "medium-6"]
    ota = reading + listening + ["hard-5", "ota-4"]

    def groups(*choices):
        return {"kind": "groups", "groups": list(choices)}

    return [
        ("First Clear", "Complete your first challenge.", {"kind": "clears", "count": 1}),
        ("Getting Rolling", "Clear 5 distinct challenges.", {"kind": "clears", "count": 5}),
        ("Double Digits", "Clear 10 distinct challenges.", {"kind": "clears", "count": 10}),
        ("Seasoned Challenger", "Clear 20 distinct challenges.", {"kind": "clears", "count": 20}),
        ("Media Sampler", "Clear challenges in any 4 of the 5 formats: manga, shows, YouTube, visual novels, and novels.",
         {"kind": "formats", "count": 4}),
        ("All-Rounder", "Clear a challenge in each format: manga, shows, YouTube, visual novels, and novels.",
         {"kind": "formats", "count": 5}),
        ("Double Feature", "Clear a reading challenge and a listening challenge.", groups(reading, listening)),
        ("Two Worlds", "Clear an Ota challenge and a Physical challenge.", groups(ota, walk + endurance)),
        ("Moving Mind", "Clear a walking challenge and an endurance challenge.", groups(walk, endurance)),
        ("The Long Game", "Complete a long-running Special challenge.", groups(long_projects)),
        ("Progress Published", "Complete the JP Progress Video challenge.", groups(["easy-5"])),
        ("Grand Line", "Complete the One Piece challenge.", groups(["medium-7"])),
        ("Into the Uncanny", "Complete the Umezu Kazuo manga challenge.", groups(["medium-8"])),
        ("Toriyama's World", "Complete the Toriyama Akira manga challenge.", groups(["medium-6"])),
        ("History Buff", "Complete the history manga challenge.", groups(["medium-5"])),
        ("Cover to Cover", "Complete Novel in 24 Hours.", groups(["hard-1"])),
        ("Page to Screen", "Clear a manga challenge and a shows challenge.", groups(manga, shows)),
        ("Quarter Century", "Clear 25 distinct challenges.", {"kind": "clears", "count": 25}),
    ]


def image_path(filename: str | None) -> Path | None:
    """Only managed PNGs are allowed; never accept a URL or an arbitrary path."""
    if not filename:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.png", filename):
        raise ValueError("Select or upload a PNG from the achievement image collection.")
    directory = upload_dir() if filename.startswith("upload-") else IMAGE_DIR
    path = directory / filename
    if path.resolve().parent != directory.resolve():
        raise ValueError("Invalid achievement image.")
    return path


def validate_rule(rule: object) -> dict:
    if not isinstance(rule, dict):
        raise ValueError("The unlock rule must be a JSON object.")
    kind = rule.get("kind")
    if kind in ("clears", "formats"):
        if set(rule) != {"kind", "count"}:
            raise ValueError("Use only kind and count for this rule.")
        count = rule["count"]
        maximum = 5 if kind == "formats" else 100000
        if type(count) is not int or not 1 <= count <= maximum:
            raise ValueError(f"Count must be an integer between 1 and {maximum}.")
    elif kind == "groups":
        if set(rule) != {"kind", "groups"}:
            raise ValueError("Use only kind and groups for this rule.")
        groups = rule["groups"]
        if not isinstance(groups, list) or not 1 <= len(groups) <= 100:
            raise ValueError("Provide at least one group of challenge IDs.")
        for group in groups:
            if not isinstance(group, list) or not 1 <= len(group) <= 100:
                raise ValueError("Each group must contain at least one challenge ID.")
            if any(not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", cid)
                   for cid in group):
                raise ValueError("Groups must contain valid challenge IDs.")
    else:
        raise ValueError("Choose clears, formats, or groups. Failure-based rules are not supported.")
    return rule


def bootstrap(conn: sqlite3.Connection) -> None:
    # Seed once, never overwrite edits or restore an administrator's removals.
    if not conn.execute("SELECT 1 FROM meta WHERE key='achievements_seeded'").fetchone():
        now = utcnow_iso()
        for position, (title, description, rule) in enumerate(_defaults(), 1):
            aid = f"achievement-{position:03}"
            conn.execute(
                "INSERT OR IGNORE INTO achievement_defs "
                "(id,title,description,rule_json,image_file,position,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (aid, title, description, json.dumps(rule), f"{aid}.png", position, now, now),
            )
        conn.execute("INSERT INTO meta(key,value) VALUES ('achievements_seeded','1')")
    # Old completion dates are not guessed. Unknown dates remain NULL.
    for row in conn.execute(
        "SELECT a.id AS attempt_id, c.* FROM attempts a "
        "JOIN challenge_defs c ON c.id=a.challenge_id "
        "WHERE a.status='completed' AND a.challenge_snapshot_json IS NULL"
    ).fetchall():
        conn.execute("UPDATE attempts SET challenge_snapshot_json=? WHERE id=?",
                     (completion_snapshot(dict(row)), row["attempt_id"]))
    sync_all(conn)


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def sync_user(conn: sqlite3.Connection, discord_id: str) -> None:
    """Reconcile eligibility in the same transaction as a completion/correction."""
    rows = conn.execute(
        "SELECT a.id,a.challenge_id,a.completed_at,a.challenge_snapshot_json,"
        "c.media_filter,c.tier "
        "FROM attempts a LEFT JOIN challenge_defs c ON c.id=a.challenge_id "
        "WHERE a.discord_id=? AND a.status='completed'", (str(discord_id),)
    ).fetchall()
    special_ids = {
        r["id"] for r in conn.execute(
            "SELECT id FROM challenge_defs WHERE LOWER(tier)='special'"
        )
    }

    def _snap(row):
        return json.loads(row["challenge_snapshot_json"] or "{}")

    def _is_special(row):
        tier = str(_snap(row).get("tier") or row["tier"] or "").lower()
        return tier == "special" or row["challenge_id"] in special_ids

    # Undated legacy completions qualify, but cannot establish a reliable unlock date.
    rows = sorted(rows, key=lambda r: (_date(r["completed_at"]) is not None,
                                      _date(r["completed_at"]) or datetime.min.replace(tzinfo=timezone.utc),
                                      r["id"]))
    seen_all, seen, formats = set(), set(), set()
    undated_all, undated_ids, undated_formats = set(), set(), set()
    for row in rows:
        if _is_special(row):
            special_ids.add(row["challenge_id"])
        if _date(row["completed_at"]) is None:
            undated_all.add(row["challenge_id"])
            if not _is_special(row):
                undated_ids.add(row["challenge_id"])
                snapshot = _snap(row)
                media = snapshot.get("media_filter", row["media_filter"])
                if media in FORMATS:
                    undated_formats.add(FORMATS[media])
    definitions = conn.execute("SELECT id,rule_json FROM achievement_defs WHERE enabled=1").fetchall()
    earned = {}
    for row in rows:
        seen_all.add(row["challenge_id"])
        snapshot = _snap(row)
        if not _is_special(row):
            seen.add(row["challenge_id"])
            media = snapshot.get("media_filter", row["media_filter"])
            if media in FORMATS:
                formats.add(FORMATS[media])
        for definition in definitions:
            aid = definition["id"]
            if aid in earned:
                continue
            rule = json.loads(definition["rule_json"])
            kind = rule["kind"]
            if kind == "groups":
                groups = []
                for group in rule["groups"]:
                    gs = set(group)
                    groups.append(gs if gs <= special_ids else gs - special_ids)
            else:
                groups = []
            qualifies = (
                len(seen) >= rule["count"] if kind == "clears" else
                len(formats) >= rule["count"] if kind == "formats" else
                all(seen_all.intersection(group) for group in groups)
            )
            if qualifies:
                date = _date(row["completed_at"])
                uncertain = (
                    bool(undated_ids) if kind == "clears" else
                    bool(undated_formats) if kind == "formats" else
                    any(undated_all.intersection(group) for group in groups)
                )
                if uncertain:
                    date = None
                earned[aid] = (date.isoformat() if date else None, row["id"])
    for definition in definitions:
        aid = definition["id"]
        if aid in earned:
            date, source = earned[aid]
            conn.execute(
                "INSERT INTO user_achievements(discord_id,achievement_id,unlocked_at,source_attempt_id) "
                "VALUES (?,?,?,?) ON CONFLICT(discord_id,achievement_id) DO UPDATE SET "
                "unlocked_at=excluded.unlocked_at, source_attempt_id=excluded.source_attempt_id",
                (str(discord_id), aid, date, source),
            )
        else:
            conn.execute("DELETE FROM user_achievements WHERE discord_id=? AND achievement_id=?",
                         (str(discord_id), aid))


def sync_all(conn: sqlite3.Connection) -> None:
    for row in conn.execute(
        "SELECT DISTINCT discord_id FROM attempts UNION SELECT DISTINCT discord_id FROM user_achievements"
    ).fetchall():
        sync_user(conn, row["discord_id"])


def list_definitions(*, include_removed: bool = False) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM achievement_defs " + ("" if include_removed else "WHERE enabled=1 ")
            + "ORDER BY position,id"
        ).fetchall()
    return [dict(row) for row in rows]


def for_user(discord_id: str) -> list[dict]:
    with db_session() as conn:
        sync_user(conn, discord_id)
        rows = conn.execute(
            "SELECT d.*,u.unlocked_at,u.source_attempt_id, "
            "u.achievement_id IS NOT NULL AS earned FROM achievement_defs d "
            "LEFT JOIN user_achievements u ON u.achievement_id=d.id AND u.discord_id=? "
            "WHERE d.enabled=1 ORDER BY earned DESC,u.unlocked_at DESC,d.position,d.id",
            (str(discord_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def save_definition(data: dict) -> str:
    aid = str(data.get("id") or uuid.uuid4())
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", aid):
        raise ValueError("Invalid achievement ID.")
    title = str(data.get("title") or "").strip()
    description = str(data.get("description") or "").strip()
    if not 1 <= len(title) <= 80 or "\n" in title or "\r" in title:
        raise ValueError("Use a single-line title of 1–80 characters.")
    if not 1 <= len(description) <= 500:
        raise ValueError("Use a requirement of 1–500 characters.")
    raw = data.get("rule_json", "")
    if isinstance(raw, str) and len(raw) > 128000:
        raise ValueError("The unlock rule is too large.")
    try:
        rule = validate_rule(json.loads(raw) if isinstance(raw, str) else raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Enter valid JSON for the unlock rule.") from exc
    filename = str(data.get("image_file") or "").strip() or None
    path = image_path(filename)
    if path and not path.is_file():
        raise ValueError("The selected image does not exist.")
    enabled = data.get("enabled", 1)
    if enabled not in (0, 1):
        raise ValueError("Invalid visibility setting.")
    if enabled and not filename:
        raise ValueError("Choose or upload artwork before publishing this achievement.")
    try:
        position = int(data.get("position", 0))
    except (ValueError, TypeError) as exc:
        raise ValueError("Catalog order must be an integer.") from exc
    if not 0 <= position <= 100000:
        raise ValueError("Catalog order must be between 0 and 100000.")
    with db_session() as conn:
        if rule["kind"] == "groups":
            required = {cid for group in rule["groups"] for cid in group}
            known = {r["id"] for r in conn.execute("SELECT id FROM challenge_defs")}
            if required - known:
                raise ValueError("Unknown challenge IDs: " + ", ".join(sorted(required - known)))
        now = utcnow_iso()
        conn.execute(
            "INSERT INTO achievement_defs "
            "(id,title,description,rule_json,image_file,enabled,position,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, "
            "description=excluded.description,rule_json=excluded.rule_json,image_file=excluded.image_file, "
            "enabled=excluded.enabled,position=excluded.position,updated_at=excluded.updated_at",
            (aid, title, description, json.dumps(rule), filename, enabled, position, now, now),
        )
        sync_all(conn)
    return aid


def remove_definition(aid: str) -> None:
    """Remove from published views, preserving the ID and historical receipts."""
    with db_session() as conn:
        if not conn.execute("UPDATE achievement_defs SET enabled=0,updated_at=? WHERE id=?",
                            (utcnow_iso(), aid)).rowcount:
            raise ValueError("Achievement not found.")
