"""Transactional catalog history, including snapshots of removed definitions."""

import json

from app.db import db_session, utcnow_iso


def snapshot(conn) -> dict[str, dict]:
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM challenge_defs")}


def record_change(conn, before, after, *, change=None, note="") -> None:
    before = dict(before) if before is not None else None
    after = dict(after) if after is not None else None
    if before == after:
        return
    kind = change or ("new" if before is None else "retired" if after is None else "updated")
    conn.execute(
        "INSERT INTO challenge_changes(challenge_id,change,before_json,after_json,note,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            (after or before)["id"], kind,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            note, utcnow_iso(),
        ),
    )


def record_import(conn, before: dict[str, dict]) -> None:
    after = snapshot(conn)
    initialized = conn.execute(
        "SELECT 1 FROM meta WHERE key='catalog_change_tracking'"
    ).fetchone()
    if not initialized:
        # Baseline the existing catalog, not a fictitious announcement of every entry.
        # The operator explicitly reported these changes when tracking was requested.
        manga = {"easy-1", "medium-1", "hard-2", "ota-2"}
        for cid, ch in after.items():
            kind = "updated" if cid in manga else "new" if (
                cid.startswith("physical-") and ch.get("category") == "physical"
            ) else None
            if ch["year"] == 2026 and kind and not conn.execute(
                "SELECT 1 FROM challenge_changes WHERE challenge_id=?", (cid,)
            ).fetchone():
                old = before.get(cid) if kind == "updated" and before.get(cid) != ch else None
                record_change(
                    conn, old, ch, change=kind,
                    note=(
                        "Initial operator report; captured pre-import definition."
                        if old else
                        "Initial operator report; previous definition was not captured."
                    ),
                )
        conn.execute("INSERT INTO meta(key,value) VALUES('catalog_change_tracking','1')")
        return
    for cid in sorted(before.keys() | after.keys()):
        record_change(conn, before.get(cid), after.get(cid))


def list_changes(*, pending_only=False, limit=None) -> list[dict]:
    query = "SELECT * FROM challenge_changes"
    if pending_only:
        query += " WHERE announced_at IS NULL"
    query += " ORDER BY id " + ("ASC" if pending_only else "DESC")
    args = []
    if limit is not None:
        query += " LIMIT ?"
        args.append(limit)
    with db_session() as conn:
        rows = [dict(r) for r in conn.execute(query, args)]
    for row in rows:
        row["before"] = json.loads(row["before_json"]) if row["before_json"] else None
        row["after"] = json.loads(row["after_json"]) if row["after_json"] else None
    return rows


def mark_announced(ids: tuple[int, ...]) -> None:
    with db_session() as conn:
        conn.executemany(
            "UPDATE challenge_changes SET announced_at=? WHERE id=? AND announced_at IS NULL",
            [(utcnow_iso(), cid) for cid in ids],
        )


def changes_text(changes: list[dict]) -> str:
    def short(value, limit):
        text = str(value)
        return text if len(text) <= limit else text[:limit] + "…"

    sections = []
    for kind, label in (("new", "New challenges"), ("updated", "Modified challenges"), ("retired", "Removed challenges")):
        blocks = []
        for event in changes:
            if event["change"] != kind:
                continue
            ch = event["after"] or event["before"]
            lines = [f"{ch['tier'].replace('_', '-')} #{ch['number']}: {ch['title']}"]
            if event["before"] is not None and event["after"] is not None:
                for field, value in event["after"].items():
                    old = event["before"].get(field)
                    if old != value:
                        # Full before/after snapshots remain in history.
                        lines.append(f"  {field}: {short(old, 300)} → {short(value, 300)}")
            else:
                lines.append(short(ch.get("description") or "", 1200))
            if event["note"]:
                lines.append(event["note"])
            blocks.append("\n".join(lines))
        if blocks:
            sections.append(f"**{label}**\n\n" + "\n\n".join(blocks))
    return "\n\n".join(sections) or "No unannounced catalog changes."


def change_draft() -> tuple[str, tuple[int, ...]]:
    chosen = []
    for event in list_changes(pending_only=True):
        if len(changes_text(chosen + [event])) > 12000:
            break
        chosen.append(event)
    return changes_text(chosen), tuple(c["id"] for c in chosen)
