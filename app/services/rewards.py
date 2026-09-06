from __future__ import annotations

import logging
from typing import Any, Optional

import discord

from app.challenge_card import (
    as_discord_file,
    avatar_bytes,
    files_as_image_embeds,
    render_challenge_card,
)
from app.config import get_settings, merge_runtime
from app.congrats import as_discord_congrats, pick_congrats
from app.db import db_session, get_all_settings
from app.poster import fetch_poster
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


async def _resolve_text_channel(
    bot: discord.Client,
    cid: str,
    guild: discord.Guild | None = None,
) -> discord.TextChannel | None:
    cid = (cid or "").strip()
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


async def resolve_announce_channel(
    bot: discord.Client,
    guild: discord.Guild | None = None,
) -> discord.TextChannel | None:
    """Main announce channel (cards + congrats on complete)."""
    rt = _runtime()
    return await _resolve_text_channel(bot, rt.get("announce_channel_id") or "", guild)


async def resolve_secondary_announce_channel(
    bot: discord.Client,
    guild: discord.Guild | None = None,
) -> discord.TextChannel | None:
    """Secondary channel — lifecycle cards only (no congrats)."""
    rt = _runtime()
    return await _resolve_text_channel(
        bot, rt.get("announce_secondary_channel_id") or "", guild
    )


def _fmt_utc(ts: int | None) -> str:
    """Absolute UTC stamp for PNG cards (ASCII only — card fonts vary)."""
    if not ts:
        return ""
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    # Wed 26 Mar 18:00 UTC — full date, no fancy unicode (· → breaks some fonts)
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')} {dt.strftime('%H:%M')} UTC"


def _ts_line(label: str, ts: int | None) -> str:
    stamp = _fmt_utc(ts)
    return f"{label} {stamp}" if stamp else ""


def _discord_when(ts: int | None, *, relative: bool = True) -> str:
    """Viewer-local Discord timestamp (each client renders their own TZ)."""
    if not ts:
        return ""
    t = int(ts)
    if relative:
        return f"<t:{t}:F> (<t:{t}:R>)"
    return f"<t:{t}:F>"


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
    media_filter: Optional[str] = None,
    poster_bytes: Optional[bytes] = None,
) -> tuple[bytes, str]:
    """Return (png_bytes, short_caption). Caption uses <t:> for local TZ."""
    name = (
        getattr(user, "display_name", None)
        or display_name
        or (str(user) if user else "Someone")
    )
    focus = (content or param or "").strip()
    av = await avatar_bytes(user)
    poster = poster_bytes
    if poster is None and focus:
        try:
            poster = await fetch_poster(focus, media_filter)
        except Exception:  # noqa: BLE001
            poster = None

    primary = ""
    secondary = ""
    footer = ""
    if kind == "start":
        primary = "Good luck"
        secondary = _ts_line("Ends", ends_at_ts) or "Window open"
    elif kind == "scheduled":
        primary = "Queued to start"
        a, b = _fmt_utc(starts_at_ts), _fmt_utc(ends_at_ts)
        if a and b:
            secondary = f"{a.removesuffix(' UTC')} -> {b}"
        elif a:
            secondary = f"Starts {a}"
        elif b:
            secondary = f"Ends {b}"
    elif kind == "complete":
        primary = "Cleared"
        if detail:
            secondary = str(detail)[:90]
        elif method:
            secondary = str(method).replace("tadoku_poll", "Tadoku").replace("test_", "")
        else:
            secondary = "Verified"
    elif kind == "fail":
        primary = "Goal not met"
        secondary = (detail or "")[:90]
    elif kind == "abandon":
        primary = "Abandoned"
        secondary = ""
    elif kind == "expired":
        primary = "Time's up"
        secondary = _ts_line("Ended", ends_at_ts) or "Window closed"
    elif kind == "active":
        primary = "In progress"
        secondary = _ts_line("Ends", ends_at_ts) or "Window open"
    else:
        primary = kind.replace("_", " ").title()
        secondary = (detail or "")[:90]

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
        poster_bytes=poster,
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
    caption = captions.get(kind, f"{mention} · {kind}")
    # Viewer-local times under the card (PNG stays absolute UTC)
    when_bits: list[str] = []
    if kind == "scheduled" and starts_at_ts:
        when_bits.append(f"Starts {_discord_when(starts_at_ts)}")
        if ends_at_ts:
            when_bits.append(f"window ends {_discord_when(ends_at_ts, relative=False)}")
    elif kind in ("start", "active") and ends_at_ts:
        when_bits.append(f"Ends {_discord_when(ends_at_ts)}")
    elif kind == "expired" and ends_at_ts:
        when_bits.append(f"Ended {_discord_when(ends_at_ts, relative=False)}")
    if when_bits:
        caption = f"{caption}\n" + " | ".join(when_bits)
    return png, caption


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
    media_filter: Optional[str] = None,
) -> bool:
    """Post lifecycle card to main + secondary announce channels.

    Congrats media is attached on **main only** (complete events).
    """
    main = await resolve_announce_channel(bot, guild)
    secondary = await resolve_secondary_announce_channel(bot, guild)
    if main is None and secondary is None:
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
            media_filter=media_filter,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("announce card build failed (%s): %s", kind, exc)
        png, caption = None, None

    who = user.mention if user is not None else (display_name or "Someone")
    focus = (content or param or "").strip()
    extra = f" · _{focus}_" if focus else ""
    text_fallback = f"{who} · **{kind}** · **{tier} #{number}: {title}**{extra}"

    # Congrats path resolved once; only attached on main
    cong_path = None
    if kind == "complete" and png is not None:
        cong_path = pick_congrats(tier=tier, content=focus)

    posted = False

    async def _send(
        channel: discord.TextChannel, *,
        with_congrats: bool,
    ) -> bool:
        nonlocal posted
        try:
            if png is None:
                await channel.send(text_fallback)
                posted = True
                return True
            files = [as_discord_file(png, f"{kind}-card.png")]
            if with_congrats and cong_path is not None:
                try:
                    files.append(as_discord_congrats(cong_path))
                except OSError as exc:
                    logger.warning("congrats file open failed: %s", exc)
            embeds = files_as_image_embeds(files, kind=kind)
            await channel.send(content=caption, files=files, embeds=embeds)
            posted = True
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "announce to #%s failed (%s): %s",
                getattr(channel, "id", "?"),
                kind,
                exc,
            )
            try:
                await channel.send(text_fallback)
                posted = True
                return True
            except discord.HTTPException as exc2:
                logger.warning("announce text fallback failed: %s", exc2)
                return False

    if main is not None:
        await _send(main, with_congrats=True)
    if secondary is not None and (main is None or secondary.id != main.id):
        await _send(secondary, with_congrats=False)

    return posted


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
