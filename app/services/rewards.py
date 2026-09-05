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
    # optional thresholds: clear_3, clear_10, etc.
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
) -> None:
    rt = _runtime()
    cid = (rt.get("announce_channel_id") or "").strip()
    if not cid or not cid.isdigit():
        return
    channel = guild.get_channel(int(cid))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(cid))
        except discord.HTTPException:
            return
    if not isinstance(channel, discord.TextChannel):
        return
    extra = f" · _{param}_" if param else ""
    text = (
        f"🎉 **{user.display_name}** completed **{tier} #{number}: {title}**{extra} "
        f"({method})"
    )
    try:
        await channel.send(text)
    except discord.HTTPException as exc:
        logger.warning("announce failed: %s", exc)
