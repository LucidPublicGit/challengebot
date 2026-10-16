#!/usr/bin/env python3
"""Migrate a NEW isolated SQLite copy and produce private reconciliation reports.

Never migrates the source, imports the catalog, sends announcements, or starts a
bot. Review reports before separately authorizing any live deployment.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reconcile_lifecycle import compare, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Existing backup or source DB opened read-only")
    parser.add_argument("--work-dir", type=Path, required=True, help="Must not already exist; private output directory")
    args = parser.parse_args()
    # No overwrite, even when a previous run failed. Each rehearsal has its own
    # immutable before/after reports and copy for inspection.
    args.work_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(args.work_dir, 0o700)
    target = args.work_dir / "migration-copy.sqlite"
    source = sqlite3.connect(args.source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    os.chmod(target, 0o600)
    before = report(target)
    (args.work_dir / "before.json").write_text(json.dumps(before, indent=2, sort_keys=True))
    from app.db import init_db
    init_db(str(target))
    after = report(target)
    # A second startup exercises restart safety without reimporting catalog.
    init_db(str(target))
    restart = report(target)
    comparison = compare(before, after)
    restart_comparison = compare(after, restart)
    for name, result in (("after", after), ("restart", restart), ("comparison", comparison), ("restart-comparison", restart_comparison)):
        path = args.work_dir / f"{name}.json"
        path.write_text(json.dumps(result, indent=2, sort_keys=True))
        os.chmod(path, 0o600)
    os.chmod(args.work_dir / "before.json", 0o600)
    print(f"Isolated migration finished. Private reports: {args.work_dir}")
    print(f"Baseline reconciliation clean: {comparison['clean']}; restart clean: {restart_comparison['clean']}")
    if not comparison["clean"] or not restart_comparison["clean"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
