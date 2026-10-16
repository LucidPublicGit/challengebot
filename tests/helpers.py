"""Shared local test helpers (temp SQLite, no Discord/Tadoku network)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.catalog import upsert_challenges_from_yaml
from app.db import configure, init_db


def setup_tmp_db(*, legacy_scoring: bool = True) -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    configure(path)
    init_db(path)
    if legacy_scoring:
        # Existing regression fixtures describe the deployed 1/10/50 policy.
        # Keep it explicit rather than changing their historical entitlements
        # when product defaults evolve. New-default tests opt out.
        from app.db import db_session
        with db_session() as conn:
            conn.execute("INSERT INTO scoring_policies(policy_json,recorded_at,actor,reason) VALUES(?, '2026-01-01T00:00:00+00:00', 'fixture', 'Historical 1/10/50 policy')",
                         ('{"formula_version":"linear_v1","multipliers":{"daily":1,"weekly":2,"monthly":10,"yearly":50,"special":1}}',))
    catalog = ROOT / "challenges" / "2026.yaml"
    upsert_challenges_from_yaml(catalog)
    return path


def cleanup_db(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(path + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
