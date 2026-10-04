from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _split_ids(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def as_bool(val: Any, default: bool = False) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def parse_fracs(raw: Any, default: tuple[float, ...]) -> tuple[float, ...]:
    """'0.25,0.5,0.75' or '25,50,75' → fractions in (0,1)."""
    if raw is None or str(raw).strip() == "":
        return default
    out: list[float] = []
    for part in str(raw).replace(";", ",").split(","):
        part = part.strip().rstrip("%")
        if not part:
            continue
        try:
            x = float(part)
        except ValueError:
            continue
        if x > 1.0:
            x = x / 100.0
        if 0.0 < x < 1.0:
            out.append(round(x, 4))
    return tuple(out) if out else default


def fracs_to_str(fracs: tuple[float, ...] | list[float]) -> str:
    return ",".join(str(int(f * 100)) if abs(f * 100 - round(f * 100)) < 1e-6 else str(f) for f in fracs)


def _load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


# Defaults (also env fallbacks)
DEFAULT_TIME_FRACS_LONG = (0.25, 0.5, 0.75)
DEFAULT_TIME_FRACS_SHORT = (0.5,)
DEFAULT_GOAL_FRACS = (0.25, 0.5, 0.75)
DEFAULT_HIGHLIGHT_KINDS = ("start", "complete", "fail")
ANNOUNCE_KIND_KEYS = (
    "start",
    "scheduled",
    "status",
    "complete",
    "fail",
    "expired",
    "abandon",
)


@dataclass
class Settings:
    discord_token: str = ""
    discord_guild_id: str = ""
    discord_client_id: str = ""
    admin_role_ids: str = ""
    database_path: str = "./data/challenges.db"
    challenge_catalog: str = "./challenges/2026.yaml"
    tadoku_api_base: str = "https://tadoku.app/api/internal/immersion"
    tadoku_contest_id: str = ""
    announce_channel_id: str = ""
    announce_secondary_channel_id: str = ""
    reward_roles: str = ""
    allow_reattempt: bool = False
    allow_honor_override: bool = True
    pages_per_volume: float = 180.0
    episode_minutes: float = 24.0
    # Status / progress (public mid-run cards off by default — start + clear only)
    status_updates_enabled: bool = False
    status_poll_minutes: float = 2.0  # 1-user Tadoku poll interval; scales × N
    status_min_interval_minutes: float = 12.0
    status_time_milestones: bool = False
    status_goal_milestones: bool = False
    status_auto_complete: bool = True
    status_time_fracs_long: str = "25,50,75"
    status_time_fracs_short: str = "50"
    status_goal_fracs: str = "25,50,75"
    status_long_window_days: float = 7.0
    status_final_hour_public: bool = False
    status_final_hour_dm: bool = True
    status_final_hour_minutes: float = 60.0
    # Announce kind toggles (fail default OFF — mistaken /confirm spam)
    announce_start: bool = True
    announce_scheduled: bool = True
    announce_status: bool = False
    announce_complete: bool = True
    announce_fail: bool = False
    announce_expired: bool = True
    announce_abandon: bool = True
    announce_congrats: bool = True
    announce_new_challenger: bool = True
    announce_shame: bool = True
    highlight_kinds: str = "start,complete,fail"
    # Monthly digest
    monthly_digest_enabled: bool = True
    monthly_digest_tz: str = ""
    monthly_digest_hour: float = 12.0
    ui_host: str = "127.0.0.1"
    ui_port: int = 8090
    ui_username: str = "admin"
    ui_password: str = ""
    log_level: str = "info"

    def admin_role_id_list(self) -> list[str]:
        return _split_ids(self.admin_role_ids)

    def reward_roles_map(self) -> dict[str, str]:
        raw = (self.reward_roles or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if v}


@lru_cache
def get_settings() -> Settings:
    _load_dotenv()
    return Settings(
        discord_token=_env("DISCORD_TOKEN"),
        discord_guild_id=_env("DISCORD_GUILD_ID"),
        discord_client_id=_env("DISCORD_CLIENT_ID"),
        admin_role_ids=_env("ADMIN_ROLE_IDS"),
        database_path=_env("DATABASE_PATH", "./data/challenges.db"),
        challenge_catalog=_env("CHALLENGE_CATALOG", "./challenges/2026.yaml"),
        tadoku_api_base=_env(
            "TADOKU_API_BASE", "https://tadoku.app/api/internal/immersion"
        ),
        tadoku_contest_id=_env(
            "TADOKU_CONTEST_ID", ""
        ),
        announce_channel_id=_env(
            "ANNOUNCE_CHANNEL_ID", ""
        ),
        announce_secondary_channel_id=_env(
            "ANNOUNCE_SECONDARY_CHANNEL_ID", ""
        ),
        reward_roles=_env("REWARD_ROLES"),
        allow_reattempt=_env_bool("ALLOW_REATTEMPT", False),
        allow_honor_override=_env_bool("ALLOW_HONOR_OVERRIDE", True),
        pages_per_volume=_env_float("PAGES_PER_VOLUME", 180.0),
        episode_minutes=_env_float("EPISODE_MINUTES", 24.0),
        status_updates_enabled=_env_bool("STATUS_UPDATES_ENABLED", False),
        status_poll_minutes=_env_float("STATUS_POLL_MINUTES", 2.0),
        status_min_interval_minutes=_env_float("STATUS_MIN_INTERVAL_MINUTES", 12.0),
        status_time_milestones=_env_bool("STATUS_TIME_MILESTONES", False),
        status_goal_milestones=_env_bool("STATUS_GOAL_MILESTONES", False),
        status_auto_complete=_env_bool("STATUS_AUTO_COMPLETE", True),
        status_time_fracs_long=_env("STATUS_TIME_FRACS_LONG", "25,50,75"),
        status_time_fracs_short=_env("STATUS_TIME_FRACS_SHORT", "50"),
        status_goal_fracs=_env("STATUS_GOAL_FRACS", "25,50,75"),
        status_long_window_days=_env_float("STATUS_LONG_WINDOW_DAYS", 7.0),
        status_final_hour_public=_env_bool("STATUS_FINAL_HOUR_PUBLIC", False),
        status_final_hour_dm=_env_bool("STATUS_FINAL_HOUR_DM", True),
        status_final_hour_minutes=_env_float("STATUS_FINAL_HOUR_MINUTES", 60.0),
        announce_start=_env_bool("ANNOUNCE_START", True),
        announce_scheduled=_env_bool("ANNOUNCE_SCHEDULED", True),
        announce_status=_env_bool("ANNOUNCE_STATUS", False),
        announce_complete=_env_bool("ANNOUNCE_COMPLETE", True),
        announce_fail=_env_bool("ANNOUNCE_FAIL", False),
        announce_expired=_env_bool("ANNOUNCE_EXPIRED", True),
        announce_abandon=_env_bool("ANNOUNCE_ABANDON", True),
        announce_congrats=_env_bool("ANNOUNCE_CONGRATS", True),
        announce_new_challenger=_env_bool("ANNOUNCE_NEW_CHALLENGER", True),
        announce_shame=_env_bool("ANNOUNCE_SHAME", True),
        highlight_kinds=_env("HIGHLIGHT_KINDS", "start,complete,fail"),
        monthly_digest_enabled=_env_bool("MONTHLY_DIGEST_ENABLED", True),
        monthly_digest_tz=_env("MONTHLY_DIGEST_TZ", ""),
        monthly_digest_hour=_env_float("MONTHLY_DIGEST_HOUR", 12.0),
        ui_host=_env("UI_HOST", "127.0.0.1"),
        ui_port=int(_env("UI_PORT", "8090") or "8090"),
        ui_username=_env("UI_USERNAME", "admin"),
        ui_password=_env("UI_PASSWORD"),
        log_level=_env("LOG_LEVEL", "info"),
    )


SETTING_KEYS = (
    "tadoku_contest_id",
    "tadoku_api_base",
    "announce_channel_id",
    "announce_secondary_channel_id",
    "allow_reattempt",
    "allow_honor_override",
    "pages_per_volume",
    "episode_minutes",
    "reward_roles_json",
    "admin_role_ids",
    "guild_id",
    "status_updates_enabled",
    "status_poll_minutes",
    "status_min_interval_minutes",
    "status_time_milestones",
    "status_goal_milestones",
    "status_auto_complete",
    "status_time_fracs_long",
    "status_time_fracs_short",
    "status_goal_fracs",
    "status_long_window_days",
    "status_final_hour_public",
    "status_final_hour_dm",
    "status_final_hour_minutes",
    "announce_start",
    "announce_scheduled",
    "announce_status",
    "announce_complete",
    "announce_fail",
    "announce_expired",
    "announce_abandon",
    "announce_congrats",
    "announce_new_challenger",
    "announce_shame",
    "highlight_kinds",
    "monthly_digest_enabled",
    "monthly_digest_tz",
    "monthly_digest_hour",
)


def _db_bool(db_map: dict[str, str], key: str, default: bool) -> bool:
    if key not in db_map:
        return default
    return as_bool(db_map[key], default)


def _db_float(db_map: dict[str, str], key: str, default: float) -> float:
    raw = db_map.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _db_str(db_map: dict[str, str], key: str, default: str) -> str:
    if key not in db_map:
        return default
    return str(db_map[key]) if db_map[key] is not None else default


def merge_runtime(settings: Settings, db_map: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "tadoku_contest_id": settings.tadoku_contest_id,
        "tadoku_api_base": settings.tadoku_api_base,
        "announce_channel_id": settings.announce_channel_id,
        "announce_secondary_channel_id": settings.announce_secondary_channel_id,
        "allow_reattempt": settings.allow_reattempt,
        "allow_honor_override": settings.allow_honor_override,
        "pages_per_volume": settings.pages_per_volume,
        "episode_minutes": settings.episode_minutes,
        "reward_roles": settings.reward_roles_map(),
        "admin_role_ids": settings.admin_role_id_list(),
        "guild_id": settings.discord_guild_id,
        "status_updates_enabled": settings.status_updates_enabled,
        "status_poll_minutes": settings.status_poll_minutes,
        "status_min_interval_minutes": settings.status_min_interval_minutes,
        "status_time_milestones": settings.status_time_milestones,
        "status_goal_milestones": settings.status_goal_milestones,
        "status_auto_complete": settings.status_auto_complete,
        "status_time_fracs_long": parse_fracs(
            settings.status_time_fracs_long, DEFAULT_TIME_FRACS_LONG
        ),
        "status_time_fracs_short": parse_fracs(
            settings.status_time_fracs_short, DEFAULT_TIME_FRACS_SHORT
        ),
        "status_goal_fracs": parse_fracs(
            settings.status_goal_fracs, DEFAULT_GOAL_FRACS
        ),
        "status_long_window_days": settings.status_long_window_days,
        "status_final_hour_public": settings.status_final_hour_public,
        "status_final_hour_dm": settings.status_final_hour_dm,
        "status_final_hour_minutes": settings.status_final_hour_minutes,
        "announce_start": settings.announce_start,
        "announce_scheduled": settings.announce_scheduled,
        "announce_status": settings.announce_status,
        "announce_complete": settings.announce_complete,
        "announce_fail": settings.announce_fail,
        "announce_expired": settings.announce_expired,
        "announce_abandon": settings.announce_abandon,
        "announce_congrats": settings.announce_congrats,
        "announce_new_challenger": settings.announce_new_challenger,
        "announce_shame": settings.announce_shame,
        "highlight_kinds": tuple(
            k.strip().lower()
            for k in (settings.highlight_kinds or "").split(",")
            if k.strip()
        )
        or DEFAULT_HIGHLIGHT_KINDS,
        "monthly_digest_enabled": settings.monthly_digest_enabled,
        "monthly_digest_tz": settings.monthly_digest_tz,
        "monthly_digest_hour": settings.monthly_digest_hour,
    }
    if db_map.get("tadoku_contest_id"):
        out["tadoku_contest_id"] = db_map["tadoku_contest_id"]
    if db_map.get("tadoku_api_base"):
        out["tadoku_api_base"] = db_map["tadoku_api_base"]
    if (db_map.get("announce_channel_id") or "").strip():
        out["announce_channel_id"] = db_map["announce_channel_id"].strip()
    if (db_map.get("announce_secondary_channel_id") or "").strip():
        out["announce_secondary_channel_id"] = db_map[
            "announce_secondary_channel_id"
        ].strip()
    out["allow_reattempt"] = _db_bool(db_map, "allow_reattempt", out["allow_reattempt"])
    out["allow_honor_override"] = _db_bool(
        db_map, "allow_honor_override", out["allow_honor_override"]
    )
    if db_map.get("pages_per_volume"):
        out["pages_per_volume"] = float(db_map["pages_per_volume"])
    if db_map.get("episode_minutes"):
        out["episode_minutes"] = float(db_map["episode_minutes"])
    if db_map.get("reward_roles_json"):
        try:
            data = json.loads(db_map["reward_roles_json"])
            if isinstance(data, dict):
                out["reward_roles"] = {
                    str(k): str(v) for k, v in data.items() if v
                }
        except json.JSONDecodeError:
            pass
    if "admin_role_ids" in db_map and db_map["admin_role_ids"] is not None:
        out["admin_role_ids"] = _split_ids(db_map["admin_role_ids"])
    if db_map.get("guild_id"):
        out["guild_id"] = db_map["guild_id"]

    out["status_updates_enabled"] = _db_bool(
        db_map, "status_updates_enabled", out["status_updates_enabled"]
    )
    out["status_poll_minutes"] = _db_float(
        db_map, "status_poll_minutes", float(out["status_poll_minutes"])
    )
    out["status_min_interval_minutes"] = _db_float(
        db_map,
        "status_min_interval_minutes",
        float(out["status_min_interval_minutes"]),
    )
    out["status_time_milestones"] = _db_bool(
        db_map, "status_time_milestones", out["status_time_milestones"]
    )
    out["status_goal_milestones"] = _db_bool(
        db_map, "status_goal_milestones", out["status_goal_milestones"]
    )
    out["status_auto_complete"] = _db_bool(
        db_map, "status_auto_complete", out["status_auto_complete"]
    )
    if "status_time_fracs_long" in db_map:
        out["status_time_fracs_long"] = parse_fracs(
            db_map.get("status_time_fracs_long"), DEFAULT_TIME_FRACS_LONG
        )
    if "status_time_fracs_short" in db_map:
        out["status_time_fracs_short"] = parse_fracs(
            db_map.get("status_time_fracs_short"), DEFAULT_TIME_FRACS_SHORT
        )
    if "status_goal_fracs" in db_map:
        out["status_goal_fracs"] = parse_fracs(
            db_map.get("status_goal_fracs"), DEFAULT_GOAL_FRACS
        )
    out["status_long_window_days"] = _db_float(
        db_map, "status_long_window_days", float(out["status_long_window_days"])
    )
    out["status_final_hour_public"] = _db_bool(
        db_map, "status_final_hour_public", out["status_final_hour_public"]
    )
    out["status_final_hour_dm"] = _db_bool(
        db_map, "status_final_hour_dm", out["status_final_hour_dm"]
    )
    out["status_final_hour_minutes"] = _db_float(
        db_map,
        "status_final_hour_minutes",
        float(out["status_final_hour_minutes"]),
    )

    for k in ANNOUNCE_KIND_KEYS:
        key = f"announce_{k}"
        out[key] = _db_bool(db_map, key, bool(out[key]))
    out["announce_congrats"] = _db_bool(
        db_map, "announce_congrats", out["announce_congrats"]
    )
    out["announce_new_challenger"] = _db_bool(
        db_map, "announce_new_challenger", out["announce_new_challenger"]
    )
    out["announce_shame"] = _db_bool(db_map, "announce_shame", out["announce_shame"])
    if (db_map.get("highlight_kinds") or "").strip():
        out["highlight_kinds"] = tuple(
            k.strip().lower()
            for k in db_map["highlight_kinds"].split(",")
            if k.strip()
        )

    out["monthly_digest_enabled"] = _db_bool(
        db_map, "monthly_digest_enabled", out["monthly_digest_enabled"]
    )
    out["monthly_digest_tz"] = _db_str(
        db_map, "monthly_digest_tz", str(out.get("monthly_digest_tz") or "")
    )
    out["monthly_digest_hour"] = _db_float(
        db_map, "monthly_digest_hour", float(out["monthly_digest_hour"])
    )
    return out


def announce_kind_enabled(rt: dict[str, Any], kind: str) -> bool:
    k = (kind or "").strip().lower()
    if k == "active":
        k = "status"
    key = f"announce_{k}"
    if key in rt:
        return bool(rt[key])
    return True
