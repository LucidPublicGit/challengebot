from __future__ import annotations

import logging
from typing import Any, Optional

import discord

from app.challenge_card import as_discord_file, avatar_bytes, render_challenge_card
from app.config import get_settings, merge_runtime
from app.db import db_session, get_all_settings
from app.services.attempts import completion_counts

logger = logging.getLogger(__name__)


def _runtime() -> dict[str, Any]:
    with db_session() as conn:
        return merge_runtime(get_settings(), get_all_settings(conn))


async def apply_rewards(
    member: discord.Member,
    *,
    tier: str,
) -> list[str]:
    """Assign configured reward roles. Returns list of applied role names/ids."""
    rt = _runtime()
    roles_map: dict[str, str] = rt.get("reward_roles") or {}
    applied: list[str] = []
    want: list[str] = []
    if tier in roles_map:
        want.append(roles_map[tier])
    if "any_clear" in roles_map:
        want.append(roles_map["any_clear"])
    counts = completion_counts(str(member.id))
    for key, rid in roles_map.items():
        if key.startswith("clear_") and key[6:].isdigit():
            if counts.get("total", 0) >= int(key[6:]):
                want.append(rid)

    for rid in want:
        if not rid:
            continue
        role = member.guild.get_role(int(rid)) if rid.isdigit() else None
        if role is None:
            logger.warning("reward role missing: %s", rid)
            continue
        if role in member.roles:
            continue
        try:
            await member.add_roles(role, reason=f"Challenge clear ({tier})")
            applied.append(role.name)
        except discord.HTTPException as exc:
            logger.warning("add_roles failed: %s", exc)
    return applied


async def resolve_announce_channel(
    bot: discord.Client,
    guild: discord.Guild | None = None,
) -> discord.TextChannel | None:
    rt = _runtime()
    cid = (rt.get("announce_channel_id") or "").strip()
    if not cid or not cid.isdigit():
        return None
    channel = None
    if guild is not None:
        channel = guild.get_channel(int(cid))
    if channel is None:
        channel = bot.get_channel(int(cid))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(cid))
        except discord.HTTPException:
            return None
    if not isinstance(channel, discord.TextChannel):
        return None
    return channel


def _ts_line(label: str, ts: int | None) -> str:
    if not ts:
        return ""
    return f"{label} <t:{ts}:F> · <t:{ts}:R>"


async def build_event_card(
    *,
    kind: str,
    user: discord.abc.User | None = None,
    display_name: str | None = None,
    title: str,
    tier: str,
    number: int,
    param: Optional[str] = None,
    content: Optional[str] = None,
    method: Optional[str] = None,
    detail: Optional[str] = None,
    ends_at_ts: int | None = None,
    starts_at_ts: int | None = None,
) -> tuple[bytes, str]:
    """Return (png_bytes, short_caption)."""
    name = (
        getattr(user, "display_name", None)
        or display_name
        or (str(user) if user else "Someone")
    )
    focus = (content or param or "").strip()
    av = await avatar_bytes(user)

    primary = ""
    secondary = ""
    footer = "ChallengeTracker"
    if kind == "start":
        primary = "Timer is running — good luck."
        secondary = _ts_line("Ends", ends_at_ts) or "Window started"
    elif kind == "scheduled":
        primary = "Locked in for later."
        secondary = _ts_line("Starts", starts_at_ts)
        if ends_at_ts:
            secondary = (secondary + " · " if secondary else "") + _ts_line(
                "Ends", ends_at_ts
            )
    elif kind == "complete":
        primary = "Cleared."
        secondary = f"Method: {method}" if method else "Verified"
        if detail:
            secondary = f"{secondary} · {detail}"[:120]
    elif kind == "fail":
        primary = "Not quite — logs don’t meet the goal yet."
        secondary = (detail or "")[:140]
    elif kind == "abandon":
        primary = "Walked away. Cowards never prosper."
        secondary = focus or "Challenge cancelled"
    elif kind == "expired":
        primary = "Timer says no."
        secondary = _ts_line("Ended", ends_at_ts) or "Window closed"
    elif kind == "active":
        primary = "In progress"
        secondary = _ts_line("Ends", ends_at_ts)
    else:
        primary = kind.replace("_", " ").title()
        secondary = detail or ""

    png = await render_challenge_card(
        kind=kind if kind in (
            "start", "scheduled", "complete", "fail", "abandon", "expired", "active", "status"
        ) else "status",
        display_name=name,
        challenge_title=title,
        tier=tier,
        number=int(number or 0),
        avatar_bytes=av,
        focus=focus,
        line_primary=primary,
        line_secondary=secondary,
        footer=footer,
    )

    mention = user.mention if user is not None else name
    captions = {
        "start": f"🏁 {mention} started a challenge",
        "scheduled": f"📅 {mention} scheduled a challenge",
        "complete": f"🎉 {mention} completed a challenge",
        "fail": f"💀 {mention} failed a check",
        "abandon": f"🏳️ {mention} abandoned a challenge",
        "expired": f"⏰ {mention} ran out of time",
        "active": f"📌 {mention} — active challenge",
    }
    return png, captions.get(kind, f"{mention} · {kind}")


async def announce(
    bot: discord.Client,
    *,
    kind: str,
    user: discord.abc.User | None = None,
    display_name: str | None = None,
    guild: discord.Guild | None = None,
    title: str,
    tier: str,
    number: int,
    param: Optional[str] = None,
    content: Optional[str] = None,
    method: Optional[str] = None,
    detail: Optional[str] = None,
    ends_at_ts: int | None = None,
    starts_at_ts: int | None = None,
) -> bool:
    """Post challenge lifecycle card to ANNOUNCE_CHANNEL_ID."""
    channel = await resolve_announce_channel(bot, guild)
    if channel is None:
        return False

    # Resolve user object if we only have an id-ish display path
    if user is None and display_name and str(display_name).isdigit():
        try:
            user = bot.get_user(int(display_name)) or await bot.fetch_user(int(display_name))
        except (TypeError, ValueError, discord.HTTPException):
            user = None

    try:
        png, caption = await build_event_card(
            kind=kind,
            user=user,
            display_name=display_name,
            title=title,
            tier=tier,
            number=number,
            param=param,
            content=content,
            method=method,
            detail=detail,
            ends_at_ts=ends_at_ts,
            starts_at_ts=starts_at_ts,
        )
        await channel.send(content=caption, file=as_discord_file(png, f"{kind}-card.png"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("announce card failed (%s): %s — falling back to text", kind, exc)
        who = user.mention if user is not None else (display_name or "Someone")
        focus = (content or param or "").strip()
        extra = f" · _{focus}_" if focus else ""
        text = f"{who} · **{kind}** · **{tier} #{number}: {title}**{extra}"
        try:
            await channel.send(text)
            return True
        except discord.HTTPException as exc2:
            logger.warning("announce failed (%s): %s", kind, exc2)
            return False


async def announce_completion(
    bot: discord.Client,
    *,
    guild: discord.Guild,
    user: discord.abc.User,
    title: str,
    tier: str,
    number: int,
    param: Optional[str],
    method: str,
    content: Optional[str] = None,
) -> None:
    await announce(
        bot,
        kind="complete",
        guild=guild,
        user=user,
        title=title,
        tier=tier,
        number=number,
        param=param,
        content=content,
        method=method,
    )
