"""Editable achievements with immutable IDs and completion-derived unlock dates."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.db import database_path, db_session, utcnow_iso

IMAGE_DIR = Path(__file__).resolve().parents[1] / "images" / "achievements"
DEFAULT_CATALOG = Path(__file__).resolve().parents[2] / "achievements" / "defaults.yaml"
MAX_CATALOG_BYTES = 512 * 1024
CATALOG_FIELDS = {"id", "title", "description", "rule", "image_file", "enabled", "position"}
FORMATS = {"manga": "manga", "shows": "shows", "youtube": "youtube",
           "vn": "vn", "book": "novel"}
SNAPSHOT_FIELDS = (
    "id", "year", "title", "category", "tier", "number", "media_filter",
    "goal_kind", "goal_amount", "time_limit_hours", "verify",
)


class _CatalogLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = super().construct_mapping(node, deep=deep)
        if len(mapping) != len(node.value):
            raise yaml.constructor.ConstructorError(
                None, None, "Duplicate mapping keys are not allowed.", node.start_mark
            )
        return mapping


def upload_dir() -> Path:
    # Beside SQLite: persists in the existing Docker data volume across rebuilds.
    return database_path().resolve().parent / "achievement_images"


def completion_snapshot(challenge: dict) -> str:
    snapshot = {key: challenge.get(key) for key in SNAPSHOT_FIELDS}
    snapshot["id"] = challenge.get("challenge_id") or challenge.get("id")
    return json.dumps(snapshot)


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
        # Challenges are seeded later in startup; shape/artwork validation happens here.
        for data in parse_catalog(DEFAULT_CATALOG.read_bytes()):
            _store_definition(conn, _definition_values(data), overwrite=False)
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


def dump_text(items: list[dict] | None = None) -> str:
    items = items if items is not None else list_definitions()
    blocks = [
        f"{a['title']}\nID: {a['id']}\n{a.get('description') or ''}"
        for a in items
    ]
    return "\n\n".join(blocks) or "No achievements found."


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


def _definition_values(data: dict, known_ids: set[str] | None = None) -> tuple:
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
    if known_ids is not None and rule["kind"] == "groups":
        required = {cid for group in rule["groups"] for cid in group}
        if required - known_ids:
            raise ValueError("Unknown challenge IDs: " + ", ".join(sorted(required - known_ids)))
    return aid, title, description, json.dumps(rule), filename, enabled, position


def _store_definition(conn: sqlite3.Connection, values: tuple, *, overwrite: bool = True) -> None:
    now = utcnow_iso()
    conflict = (
        "DO UPDATE SET title=excluded.title, description=excluded.description,"
        "rule_json=excluded.rule_json,image_file=excluded.image_file,"
        "enabled=excluded.enabled,position=excluded.position,updated_at=excluded.updated_at"
        if overwrite else "DO NOTHING"
    )
    conn.execute(
        "INSERT INTO achievement_defs "
        "(id,title,description,rule_json,image_file,enabled,position,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) " + conflict,
        (*values, now, now),
    )


def save_definition(data: dict) -> str:
    with db_session() as conn:
        known = {r["id"] for r in conn.execute("SELECT id FROM challenge_defs")}
        values = _definition_values(data, known)
        _store_definition(conn, values)
        sync_all(conn)
    return values[0]


def parse_catalog(content: bytes) -> list[dict]:
    """Read a bounded, versioned definition catalog; no user data or executable YAML."""
    if len(content) > MAX_CATALOG_BYTES:
        raise ValueError("Achievement catalogs must be 512 KB or smaller.")
    try:
        document = yaml.load(content.decode("utf-8"), Loader=_CatalogLoader)
    except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError("Upload a valid UTF-8 YAML catalog.") from exc
    if (
        not isinstance(document, dict) or set(document) != {"version", "achievements"}
        or type(document["version"]) is not int or document["version"] != 1
    ):
        raise ValueError("Use version: 1 and an achievements list.")
    entries = document["achievements"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
        raise ValueError("Provide between 1 and 1000 achievement definitions.")
    seen, definitions = set(), []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != CATALOG_FIELDS:
            raise ValueError("Each achievement needs exactly: " + ", ".join(sorted(CATALOG_FIELDS)))
        if any(not isinstance(entry[key], str) for key in ("id", "title", "description")):
            raise ValueError("Achievement IDs, titles, and descriptions must be strings.")
        if not entry["id"] or entry["id"] in seen:
            raise ValueError("Achievement IDs must be nonempty and unique.")
        if (
            type(entry["enabled"]) is not bool or type(entry["position"]) is not int
            or (entry["image_file"] is not None and not isinstance(entry["image_file"], str))
        ):
            raise ValueError("Use true/false visibility, an integer position, and a PNG filename or null.")
        seen.add(entry["id"])
        data = {key: entry[key] for key in CATALOG_FIELDS - {"rule"}}
        data["rule_json"] = entry["rule"]
        _definition_values(data)
        definitions.append(data)
    return definitions


def export_catalog() -> str:
    """Export definitions, including removed ones, never member activity or receipts."""
    entries = []
    for item in list_definitions(include_removed=True):
        entries.append(dict(
            id=item["id"], title=item["title"], description=item["description"],
            rule=json.loads(item["rule_json"]), image_file=item["image_file"],
            enabled=bool(item["enabled"]), position=item["position"],
        ))
    return yaml.safe_dump(dict(version=1, achievements=entries), sort_keys=False, allow_unicode=True)


def import_catalog(content: bytes) -> int:
    """Explicit atomic merge by permanent ID. Omitted definitions are untouched."""
    definitions = parse_catalog(content)
    with db_session() as conn:
        known = {r["id"] for r in conn.execute("SELECT id FROM challenge_defs")}
        values = [_definition_values(data, known) for data in definitions]
        for value in values:
            _store_definition(conn, value)
        sync_all(conn)
    return len(definitions)


def remove_definition(aid: str) -> None:
    """Remove from published views, preserving the ID and historical receipts."""
    with db_session() as conn:
        if not conn.execute("UPDATE achievement_defs SET enabled=0,updated_at=? WHERE id=?",
                            (utcnow_iso(), aid)).rowcount:
            raise ValueError("Achievement not found.")
