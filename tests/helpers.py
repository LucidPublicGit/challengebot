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


def setup_tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    configure(path)
    init_db(path)
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
