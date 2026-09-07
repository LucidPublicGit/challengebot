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


@dataclass
class Settings:
    discord_token: str = ""
    discord_guild_id: str = ""
    discord_client_id: str = ""
    admin_role_ids: str = ""
    database_path: str = "./data/challenges.db"
    challenge_catalog: str = "./challenges/2026.yaml"
    tadoku_api_base: str = "https://tadoku.app/api/internal/immersion"
    tadoku_contest_id: str = "90e6509e-bc32-49d4-936d-ea0776eee602"
    # Highlights: start + complete/fail (+ congrats on complete)
    announce_channel_id: str = "1187153489156124732"
    # Log: schedule, start, status, complete/fail, abandon, expired, …
    announce_secondary_channel_id: str = "1523503572652916786"
    reward_roles: str = ""
    allow_reattempt: bool = False
    # Users may complete tadoku challenges via honor when logs fail / mismatch
    allow_honor_override: bool = True
    pages_per_volume: float = 180.0
    episode_minutes: float = 24.0
    # Automatic active-challenge status posts (time + Tadoku goal milestones)
    status_updates_enabled: bool = True
    status_poll_minutes: float = 10.0  # Tadoku poll cadence
    status_min_interval_minutes: float = 12.0  # min gap between posts per attempt
    ui_host: str = "0.0.0.0"
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
            "TADOKU_CONTEST_ID", "90e6509e-bc32-49d4-936d-ea0776eee602"
        ),
        announce_channel_id=_env(
            "ANNOUNCE_CHANNEL_ID", "1187153489156124732"
        ),
        announce_secondary_channel_id=_env(
            "ANNOUNCE_SECONDARY_CHANNEL_ID", "1523503572652916786"
        ),
        reward_roles=_env("REWARD_ROLES"),
        allow_reattempt=_env_bool("ALLOW_REATTEMPT", False),
        allow_honor_override=_env_bool("ALLOW_HONOR_OVERRIDE", True),
        pages_per_volume=_env_float("PAGES_PER_VOLUME", 180.0),
        episode_minutes=_env_float("EPISODE_MINUTES", 24.0),
        status_updates_enabled=_env_bool("STATUS_UPDATES_ENABLED", True),
        status_poll_minutes=_env_float("STATUS_POLL_MINUTES", 10.0),
        status_min_interval_minutes=_env_float("STATUS_MIN_INTERVAL_MINUTES", 12.0),
        ui_host=_env("UI_HOST", "0.0.0.0"),
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
)


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
    if "allow_reattempt" in db_map:
        out["allow_reattempt"] = db_map["allow_reattempt"].lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    if "allow_honor_override" in db_map:
        out["allow_honor_override"] = db_map["allow_honor_override"].lower() in (
            "1",
            "true",
            "yes",
            "on",
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
    return out
