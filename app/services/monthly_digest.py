"""1st-of-month noon post to the log channel: leaderboard / shame / new year."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord

from app.catalog import challenge_count
from app.challenge_card import (
    as_discord_file,
    avatar_bytes,
    files_as_image_embeds,
    render_leaderboard_card,
)
from app.config import get_settings, merge_runtime
from app.congrats import as_discord_congrats
from app.db import db_session, get_all_settings, get_setting, set_setting
from app.services import attempts as att_svc
from app.services.rewards import resolve_secondary_announce_channel
from app.shame import (
    empty_month_shame,
    new_year_mod_shame,
    new_year_welcome,
    pick_shame_image,
)

logger = logging.getLogger(__name__)

_LAST_KEY = "last_monthly_digest"  # YYYY-MM


def _runtime() -> dict[str, Any]:
    try:
        with db_session() as conn:
            return merge_runtime(get_settings(), get_all_settings(conn))
    except Exception:  # noqa: BLE001
        return merge_runtime(get_settings(), {})


def _tz():
    """Local clock by default. Optional settings: IANA name or UTC±HH[:MM]."""
    name = str(_runtime().get("monthly_digest_tz") or "").strip()
    if not name:
        try:
            with db_session() as conn:
                name = (get_setting(conn, "monthly_digest_tz", "") or "").strip()
        except Exception:  # noqa: BLE001
            name = ""
    if not name:
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        pass
    m = re.fullmatch(r"UTC([+-])(\d{1,2})(?::?(\d{2}))?", name, re.I)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        mins = int(m.group(3) or 0)
        return timezone(sign * timedelta(hours=hours, minutes=mins))
    return datetime.now().astimezone().tzinfo or timezone.utc


def now_local(now: datetime | None = None) -> datetime:
    """Wall clock for digest. Aware `now` keeps its own tz (tests); else server tz."""
    if now is not None:
        if now.tzinfo is None:
            return now.replace(tzinfo=_tz())
        return now
    return datetime.now(_tz())


def month_key(dt: datetime) -> str:
    d = now_local(dt)
    return f"{d.year:04d}-{d.month:02d}"


def is_due(now: datetime | None = None, *, last_key: str | None = None) -> bool:
    """True once we're at/after the 1st <hour> local and haven't posted this month."""
    rt = _runtime()
    if not rt.get("monthly_digest_enabled", True):
        return False
    try:
        hour = int(float(rt.get("monthly_digest_hour") or 12))
    except (TypeError, ValueError):
        hour = 12
    hour = max(0, min(23, hour))
    d = now_local(now)
    key = month_key(d)
    if last_key is None:
        with db_session() as conn:
            last_key = get_setting(conn, _LAST_KEY, "")
    if (last_key or "") >= key:
        return False
    # Catch-up any day after configured hour on/after the 1st of this month
    if d.day >= 1 and (d.day > 1 or d.hour >= hour):
        return True
    return False


def classify(
    now: datetime | None = None,
    *,
    completions: int | None = None,
    year_challenges: int | None = None,
) -> str:
    """
    leaderboard | empty_shame | new_year_ready | new_year_missing
    Jan 1+ → new-year branch; other months → board or empty shame.
    """
    d = now_local(now)
    if completions is None:
        completions = att_svc.total_completions()
    if year_challenges is None:
        year_challenges = challenge_count(d.year)

    if d.month == 1:
        if year_challenges > 0:
            return "new_year_ready"
        return "new_year_missing"
    if completions > 0:
        return "leaderboard"
    return "empty_shame"


def mark_posted(key: str) -> None:
    with db_session() as conn:
        set_setting(conn, _LAST_KEY, key)


async def _build_leaderboard_entries(
    bot: discord.Client,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    uids = [str(r["discord_id"]) for r in rows]
    best_map = att_svc.highest_completed_tiers(uids)
    entries: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        uid = str(r["discord_id"])
        name = r.get("display_name") or uid
        av = None
        if uid.isdigit():
            try:
                u = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
                if u is not None:
                    name = u.display_name
                    av = await avatar_bytes(u)
            except (discord.HTTPException, ValueError):
                pass
        entries.append(
            {
                "rank": i + 1,
                "name": name,
                "clears": int(r.get("clears") or 0),
                "points": int(r.get("points") or r.get("n") or 0),
                "fails": int(r.get("fails") or 0),
                "avatar": av,
                "best_tier": best_map.get(uid),
            }
        )
    return entries


async def run_monthly_digest(bot: discord.Client) -> Optional[str]:
    """Post if due. Returns kind posted, or None if skipped."""
    if not is_due():
        return None

    d = now_local()
    key = month_key(d)
    kind = classify(d)
    month_label = d.strftime("%B %Y")

    guild = None
    with db_session() as conn:
        rt = merge_runtime(get_settings(), get_all_settings(conn))
    gid = rt.get("guild_id") or get_settings().discord_guild_id
    if gid and str(gid).isdigit():
        guild = bot.get_guild(int(gid))

    channel = await resolve_secondary_announce_channel(bot, guild)
    if channel is None:
        logger.warning("monthly digest: no log channel configured")
        return None

    try:
        if kind == "leaderboard":
            rows = att_svc.top_completers(15)
            if not rows:
                kind = "empty_shame"
            else:
                entries = await _build_leaderboard_entries(bot, rows)
                png = await render_leaderboard_card(
                    title=f"{month_label} · board",
                    entries=entries,
                    footer=(
                        f"{len(entries)} ranked · by points · "
                        "ota 4 · hard 3 · med 2 · easy 1"
                    ),
                )
                lb = as_discord_file(png, "monthly-leaderboard.png")
                caption = f"📊 **Monthly leaderboard · {month_label}**"
                await channel.send(
                    content=caption,
                    files=[lb],
                    embeds=files_as_image_embeds([lb], kind="leaderboard"),
                )
                mark_posted(key)
                logger.info("monthly digest posted kind=%s key=%s", kind, key)
                return kind

        if kind == "empty_shame":
            caption = empty_month_shame(month_label)
            files = []
            embeds = []
            img = pick_shame_image()
            if img is not None:
                f = as_discord_congrats(img)
                files.append(f)
                embeds = files_as_image_embeds(files, kind="fail")
            await channel.send(
                content=caption,
                files=files or discord.utils.MISSING,
                embeds=embeds or discord.utils.MISSING,
            )
            mark_posted(key)
            logger.info("monthly digest posted kind=%s key=%s", kind, key)
            return kind

        if kind == "new_year_ready":
            n = challenge_count(d.year)
            caption = new_year_welcome(d.year, n)
            await channel.send(content=caption)
            mark_posted(key)
            logger.info("monthly digest posted kind=%s key=%s", kind, key)
            return kind

        # new_year_missing
        caption = new_year_mod_shame(d.year)
        files = []
        embeds = []
        img = pick_shame_image()
        if img is not None:
            f = as_discord_congrats(img)
            files.append(f)
            embeds = files_as_image_embeds(files, kind="fail")
        await channel.send(
            content=caption,
            files=files or discord.utils.MISSING,
            embeds=embeds or discord.utils.MISSING,
        )
        mark_posted(key)
        logger.info("monthly digest posted kind=%s key=%s", kind, key)
        return kind
    except Exception as exc:  # noqa: BLE001
        logger.warning("monthly digest failed: %s", exc)
        return None
