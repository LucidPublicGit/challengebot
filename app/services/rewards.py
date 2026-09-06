from __future__ import annotations

import logging
from typing import Any, Optional

import discord

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


def _challenge_label(tier: str, number: int, title: str) -> str:
    return f"**{tier} #{number}: {title}**"


def _focus_bit(content: Optional[str] = None, param: Optional[str] = None) -> str:
    focus = (content or param or "").strip()
    return f" · _{focus}_" if focus else ""


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
    """
    Post challenge lifecycle events to ANNOUNCE_CHANNEL_ID.
    kind: start | scheduled | complete | fail | abandon | expired
    """
    channel = await resolve_announce_channel(bot, guild)
    if channel is None:
        return False

    who = user.mention if user is not None else (display_name or "Someone")
    label = _challenge_label(tier, number, title)
    focus = _focus_bit(content, param)

    if kind == "start":
        deadline = f" · ends <t:{ends_at_ts}:R>" if ends_at_ts else ""
        text = f"🏁 {who} started {label}{focus}{deadline}"
    elif kind == "scheduled":
        when = f" · starts <t:{starts_at_ts}:F> (<t:{starts_at_ts}:R>)" if starts_at_ts else ""
        text = f"📅 {who} scheduled {label}{focus}{when}"
    elif kind == "complete":
        method_s = f" ({method})" if method else ""
        text = f"🎉 {who} completed {label}{focus}{method_s}"
    elif kind == "fail":
        extra = f"\n_{detail}_" if detail else ""
        text = f"💀 {who} tried to confirm {label}{focus}… and **failed**.{extra}"
    elif kind == "abandon":
        text = f"🏳️ {who} abandoned {label}{focus}. Cowards never prosper."
    elif kind == "expired":
        text = f"⏰ {who} ran out of time on {label}{focus}. Timer says no."
    else:
        text = f"{who} · {kind} · {label}{focus}"

    try:
        await channel.send(text)
        return True
    except discord.HTTPException as exc:
        logger.warning("announce failed (%s): %s", kind, exc)
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
