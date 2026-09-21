"""Rolling SQLite + settings backups: one daily / weekly / monthly slot each."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.config import get_settings
from app.db import db_session, get_all_settings, get_setting, set_setting

logger = logging.getLogger(__name__)

# Fixed slot filenames (always replace previous of same cadence).
SLOT_DAILY = "challenges-daily.db"
SLOT_WEEKLY = "challenges-weekly.db"
SLOT_MONTHLY = "challenges-monthly.db"
SLOT_SETTINGS_WEEKLY = "settings-weekly.json"

KEY_DAILY = "last_backup_daily"  # YYYY-MM-DD
KEY_WEEKLY = "last_backup_weekly"  # YYYY-Www
KEY_MONTHLY = "last_backup_monthly"  # YYYY-MM
KEY_SETTINGS_WEEKLY = "last_backup_settings_weekly"  # YYYY-Www
KEY_SIZE_WARN = "last_backup_size_warn"  # human note

# ponytail: single hard ceiling; raise if real DBs grow past this
WARN_BYTES = 25 * 1024 * 1024  # 25 MB


def backup_dir(database_path: str | Path | None = None) -> Path:
    db = Path(database_path or get_settings().database_path).expanduser()
    return (db.resolve().parent / "backups")


def source_db(database_path: str | Path | None = None) -> Path:
    return Path(database_path or get_settings().database_path).expanduser().resolve()


def _now_local(now: datetime | None = None) -> datetime:
    if now is not None:
        if now.tzinfo is None:
            return now
        return now.astimezone()
    return datetime.now().astimezone()


def day_key(now: datetime | None = None) -> str:
    d = _now_local(now)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def week_key(now: datetime | None = None) -> str:
    d = _now_local(now)
    iso = d.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def month_key(now: datetime | None = None) -> str:
    d = _now_local(now)
    return f"{d.year:04d}-{d.month:02d}"


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _atomic_write_bytes(dest: Path, data: bytes) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dest.stem + "-", suffix=".tmp", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest.stat().st_size


def snapshot_sqlite(src: Path, dest: Path) -> int:
    """Consistent online copy via sqlite backup API → fixed dest path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dest.stem + "-", suffix=".tmp", dir=str(dest.parent))
    os.close(fd)
    try:
        src_conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
        try:
            dst_conn = sqlite3.connect(tmp)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest.stat().st_size


def snapshot_settings_json(dest: Path) -> int:
    with db_session() as conn:
        data = get_all_settings(conn)
    payload = {
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "settings": data,
    }
    raw = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    return _atomic_write_bytes(dest, raw)


def _size_warn(label: str, path: Path, size: int) -> Optional[str]:
    if size <= WARN_BYTES:
        return None
    msg = (
        f"{label} backup large: {_fmt_size(size)} "
        f"(limit {_fmt_size(WARN_BYTES)}) → {path.name}"
    )
    logger.warning(msg)
    return msg


def run_due_backups(
    *,
    now: datetime | None = None,
    database_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    Write/replace due slots. Idempotent within the same day/week/month keys.
    Returns {done: [...], skipped: [...], warnings: [...], files: {...}}.
    """
    src = source_db(database_path)
    out_dir = backup_dir(database_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    d_key = day_key(now)
    w_key = week_key(now)
    m_key = month_key(now)

    with db_session() as conn:
        last_d = get_setting(conn, KEY_DAILY, "")
        last_w = get_setting(conn, KEY_WEEKLY, "")
        last_m = get_setting(conn, KEY_MONTHLY, "")
        last_sw = get_setting(conn, KEY_SETTINGS_WEEKLY, "")

    plan: list[tuple[str, str, str, str]] = []
    # (kind, period_key, last_key, filename) — kind drives writer
    if (last_d or "") < d_key:
        plan.append(("db", d_key, KEY_DAILY, SLOT_DAILY))
    if (last_w or "") < w_key:
        plan.append(("db", w_key, KEY_WEEKLY, SLOT_WEEKLY))
    if (last_m or "") < m_key:
        plan.append(("db", m_key, KEY_MONTHLY, SLOT_MONTHLY))
    if (last_sw or "") < w_key:
        plan.append(("settings", w_key, KEY_SETTINGS_WEEKLY, SLOT_SETTINGS_WEEKLY))

    done: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []
    files: dict[str, dict[str, Any]] = {}

    if not plan:
        return {
            "done": done,
            "skipped": ["all current"],
            "warnings": warnings,
            "files": files,
        }

    if not src.is_file() and any(k == "db" for k, *_ in plan):
        logger.warning("backup skipped: source DB missing %s", src)
        return {
            "done": done,
            "skipped": ["missing source db"],
            "warnings": [f"Source DB missing: {src}"],
            "files": files,
        }

    for kind, period, setting_key, filename in plan:
        dest = out_dir / filename
        try:
            if kind == "db":
                size = snapshot_sqlite(src, dest)
            else:
                size = snapshot_settings_json(dest)
            with db_session() as conn:
                set_setting(conn, setting_key, period)
            label = filename
            done.append(f"{label}@{period}")
            files[filename] = {"path": str(dest), "bytes": size, "period": period}
            w = _size_warn(label, dest, size)
            if w:
                warnings.append(w)
                with db_session() as conn:
                    set_setting(conn, KEY_SIZE_WARN, w)
            logger.info("Backup wrote %s (%s) period=%s", dest, _fmt_size(size), period)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Backup failed for %s", filename)
            skipped.append(f"{filename}: {exc}")

    return {"done": done, "skipped": skipped, "warnings": warnings, "files": files}


def status(*, database_path: str | Path | None = None) -> dict[str, Any]:
    """Admin UI: slot files + last keys + any size warning."""
    out_dir = backup_dir(database_path)
    with db_session() as conn:
        keys = {
            "daily": get_setting(conn, KEY_DAILY, ""),
            "weekly": get_setting(conn, KEY_WEEKLY, ""),
            "monthly": get_setting(conn, KEY_MONTHLY, ""),
            "settings_weekly": get_setting(conn, KEY_SETTINGS_WEEKLY, ""),
            "size_warn": get_setting(conn, KEY_SIZE_WARN, ""),
        }
    slots = (
        ("daily", SLOT_DAILY),
        ("weekly", SLOT_WEEKLY),
        ("monthly", SLOT_MONTHLY),
        ("settings_weekly", SLOT_SETTINGS_WEEKLY),
    )
    files = []
    for kind, name in slots:
        p = out_dir / name
        entry: dict[str, Any] = {
            "kind": kind,
            "name": name,
            "exists": p.is_file(),
            "bytes": p.stat().st_size if p.is_file() else 0,
            "mtime": None,
            "period": keys.get(kind, ""),
            "over_limit": False,
        }
        if entry["exists"]:
            entry["mtime"] = datetime.fromtimestamp(
                p.stat().st_mtime, tz=timezone.utc
            ).replace(microsecond=0).isoformat()
            entry["over_limit"] = entry["bytes"] > WARN_BYTES
            entry["size_label"] = _fmt_size(entry["bytes"])
        else:
            entry["size_label"] = "—"
        files.append(entry)
    return {
        "dir": str(out_dir),
        "warn_bytes": WARN_BYTES,
        "warn_label": _fmt_size(WARN_BYTES),
        "keys": keys,
        "files": files,
        "any_over_limit": any(f["over_limit"] for f in files),
    }


if __name__ == "__main__":
    import tempfile as _tf

    from app.db import init_db

    td = _tf.mkdtemp()
    dbp = str(Path(td) / "challenges.db")
    init_db(dbp)
    with db_session() as c:
        set_setting(c, "x", "1")
    r1 = run_due_backups(database_path=dbp)
    assert r1["done"], r1
    r2 = run_due_backups(database_path=dbp)
    assert not r2["done"], r2
    st = status(database_path=dbp)
    assert st["files"][0]["exists"]
    print("db_backup ok", r1["done"])
