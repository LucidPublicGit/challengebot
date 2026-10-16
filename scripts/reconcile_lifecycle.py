#!/usr/bin/env python3
"""Read-only lifecycle migration reconciliation. Reports are private, not commits.

Does not import app.db/init_db, run migrations, load .env, or change journal mode.
Run against a consistent SQLite backup, not a copied main file missing its WAL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

TIER_POINTS = {"easy": 1, "medium": 2, "hard": 3, "ota_master": 4}


def report(path: Path) -> dict:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        conn.execute("BEGIN")  # one consistent read snapshot
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {t: {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')} for t in tables if t in {
            "attempts", "award_ledger", "badge_receipts", "challenge_defs"}}
        statuses = {r[0]: r[1] for r in conn.execute("SELECT status,COUNT(*) FROM attempts GROUP BY status")}
        definitions = [dict(r) for r in conn.execute("SELECT * FROM challenge_defs")]
        defs = {r["id"]: r for r in definitions}
        year = max((r["year"] for r in definitions), default=None)
        roster = {tier: {r["id"] for r in definitions if r["year"] == year and r["tier"].lower() == tier} for tier in TIER_POINTS}
        members: dict[str, dict] = {}
        orphaned = 0
        unknown_completion = 0
        for row in conn.execute("SELECT * FROM attempts"):
            a = dict(row)
            m = members.setdefault(a["discord_id"], {"attempts": 0, "clears": 0, "legacy_query_points": 0, "done": set()})
            m["attempts"] += 1
            c = defs.get(a["challenge_id"])
            orphaned += c is None
            if a["status"] != "completed":
                continue
            m["clears"] += 1
            unknown_completion += not a.get("actual_completed_at") if "actual_completed_at" in columns["attempts"] else 1
            if c:
                m["legacy_query_points"] += int(c["points"]) if c.get("points") is not None else TIER_POINTS.get(c["tier"].lower(), 0)
                m["done"].add(c["id"])
        for m in members.values():
            m["legacy_query_points"] += sum(2 * TIER_POINTS[t] for t, ids in roster.items() if ids and ids <= m["done"])
            del m["done"]
        ledger_total = None
        if "award_ledger" in tables and {"discord_id", "effective_points"} <= columns["award_ledger"]:
            ledger_total = 0
            for r in conn.execute("SELECT discord_id,SUM(effective_points) FROM award_ledger GROUP BY discord_id"):
                members.setdefault(r[0], {"attempts": 0, "clears": 0, "legacy_query_points": 0})["ledger_points"] = r[1]
                ledger_total += r[1]
        for m in members.values():
            if ledger_total is not None:
                m.setdefault("ledger_points", 0)
            m["effective_points"] = m.get("ledger_points", m["legacy_query_points"])
        # Hashes permit before/after matching without printing account IDs/names.
        private_members = {hashlib.sha256(str(k).encode()).hexdigest(): v for k, v in members.items()}
        unknown = {}
        for field in ("rules_snapshot_json", "scoring_snapshot_json", "edition_id", "original_deadline", "provenance"):
            if field in columns["attempts"]:
                unknown[field] = conn.execute(f'SELECT COUNT(*) FROM attempts WHERE "{field}" IS NULL').fetchone()[0]
        return {
            "format": 1, "warning": "Private local reconciliation; hashes still represent member activity. Do not commit.",
            "integrity": [r[0] for r in conn.execute("PRAGMA integrity_check")],
            "foreign_key_errors": [list(r) for r in conn.execute("PRAGMA foreign_key_check")],
            "attempt_statuses": statuses, "attempt_count": sum(statuses.values()),
            "completion_count": statuses.get("completed", 0), "orphaned_challenge_references": orphaned,
            "unknown_actual_completion_times": unknown_completion, "unknown_metadata": unknown,
            "migration_review_required": [json.loads(r[0]) for r in conn.execute("SELECT value FROM meta WHERE key='lifecycle_review_required'")] if 'meta' in tables else [],
            "catalog_count": len(definitions), "legacy_query_catalog_year": year,
            "legacy_query_points": sum(m["legacy_query_points"] for m in members.values()),
            "ledger_points": ledger_total,
            "effective_points": sum(m["effective_points"] for m in members.values()),
            "receipt_counts": {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in (
                "badge_receipts", "daily_rosters", "award_ledger", "point_adjustments", "admin_operations") if t in tables},
            "members": private_members,
            "interpretation": "Legacy query is mutable-definition/current-max-year reconstruction, not original advertised awards. Unknown actual completion dates are not inferred from verification timestamps.",
        }
    finally:
        conn.close()


def compare(before: dict, after: dict) -> dict:
    differences = {}
    for key in ("attempt_count", "completion_count", "attempt_statuses", "orphaned_challenge_references", "effective_points"):
        if before.get(key) != after.get(key):
            differences[key] = {"before": before.get(key), "after": after.get(key)}
    member_deltas = {}
    for member in before.get("members", {}).keys() | after.get("members", {}).keys():
        b = before.get("members", {}).get(member, {})
        a = after.get("members", {}).get(member, {})
        delta = a.get("effective_points", 0) - b.get("effective_points", 0)
        if delta:
            member_deltas[member] = delta
    return {"differences_requiring_explanation": differences, "member_point_deltas": member_deltas,
            "clean": not differences and not member_deltas and after["integrity"] == ["ok"] and not after["foreign_key_errors"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Private local report path; default stdout")
    parser.add_argument("--compare", type=Path, help="Before report; exit 2 for unexplained changes")
    args = parser.parse_args()
    result = report(args.database)
    if args.compare:
        result["comparison"] = compare(json.loads(args.compare.read_text()), result)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        # Refuse accidental overwrite of the database or a baseline report.
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(text)
        args.output.chmod(0o600)
    else:
        print(text, end="")
    if result.get("comparison", {}).get("clean") is False:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
