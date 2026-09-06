from __future__ import annotations

import logging
from typing import Any, Optional

import discord

from app.challenge_card import (
    as_discord_file,
    avatar_bytes,
    files_as_image_embeds,
    render_challenge_card,
    tier_caption_mark,
)
from app.config import get_settings, merge_runtime
from app.congrats import as_discord_framed_congrats, pick_congrats
from app.db import db_session, get_all_settings
from app.new_challenger import as_discord_new_challenger, render_new_challenger
from app.poster import fetch_poster
from app.services.attempts import completion_counts, display_content, failure_count
from app.shame import format_shame_caption

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
    """Highlights channel: START + COMPLETE/FAIL (congrats on complete)."""
    rt = _runtime()
    return await _resolve_text_channel(bot, rt.get("announce_channel_id") or "", guild)


async def resolve_secondary_announce_channel(
    bot: discord.Client,
    guild: discord.Guild | None = None,
) -> discord.TextChannel | None:
    """Log channel: schedule, start, status, complete/fail, and everything else."""
    rt = _runtime()
    return await _resolve_text_channel(
        bot, rt.get("announce_secondary_channel_id") or "", guild
    )


# Which kinds go to the highlights (main) channel
_HIGHLIGHT_KINDS = frozenset({"start", "complete", "fail"})
# Log channel gets every kind we announce


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


def _fmt_duration_hours(hours: float | None) -> str:
    """Human window length for cards/captions (e.g. 24 hours, 3 days)."""
    if hours is None:
        return ""
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return ""
    if h <= 0:
        return ""
    if abs(h - round(h)) < 1e-6:
        hi = int(round(h))
        if hi >= 48 and hi % 24 == 0:
            d = hi // 24
            return "1 day" if d == 1 else f"{d} days"
        if hi == 1:
            return "1 hour"
        return f"{hi} hours"
    if h < 1:
        m = max(1, int(round(h * 60)))
        return f"{m} min"
    # one decimal if needed
    return f"{h:.1f}".rstrip("0").rstrip(".") + " hours"


def _duration_from_window(
    duration_hours: float | None,
    starts_at_ts: int | None,
    ends_at_ts: int | None,
) -> str:
    hours = duration_hours
    if hours is None and starts_at_ts and ends_at_ts:
        try:
            hours = (int(ends_at_ts) - int(starts_at_ts)) / 3600.0
        except (TypeError, ValueError):
            hours = None
    return _fmt_duration_hours(hours)


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
    line_primary: Optional[str] = None,
    line_secondary: Optional[str] = None,
    ends_at_ts: int | None = None,
    starts_at_ts: int | None = None,
    media_filter: Optional[str] = None,
    poster_bytes: Optional[bytes] = None,
    skip_image: bool = False,
    duration_hours: float | None = None,
) -> tuple[Optional[bytes], str]:
    """Return (png_bytes|None, short_caption). Caption uses <t:> for local TZ."""
    name = (
        getattr(user, "display_name", None)
        or display_name
        or (str(user) if user else "Someone")
    )
    focus = (content or param or "").strip()
    dur = _duration_from_window(duration_hours, starts_at_ts, ends_at_ts)

    primary = ""
    secondary = ""
    footer = ""
    if kind == "start":
        primary = "Good luck"
        bits = [b for b in (dur, _ts_line("Ends", ends_at_ts) or "Window open") if b]
        secondary = " · ".join(bits)
    elif kind == "scheduled":
        primary = "Queued to start"
        a, b = _fmt_utc(starts_at_ts), _fmt_utc(ends_at_ts)
        if a and b:
            window = f"{a.removesuffix(' UTC')} -> {b}"
        elif a:
            window = f"Starts {a}"
        elif b:
            window = f"Ends {b}"
        else:
            window = ""
        bits = [b for b in (dur, window) if b]
        secondary = " · ".join(bits)
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
    elif kind in ("active", "status"):
        primary = (line_primary or "In progress").strip() or "In progress"
        if line_secondary:
            secondary = str(line_secondary)[:90]
        elif detail:
            secondary = str(detail)[:90]
        else:
            bits = [b for b in (dur, _ts_line("Ends", ends_at_ts) or "Window open") if b]
            secondary = " · ".join(bits)
    else:
        primary = kind.replace("_", " ").title()
        secondary = (detail or "")[:90]

    if line_primary:
        primary = str(line_primary).strip()[:80] or primary
    if line_secondary:
        secondary = str(line_secondary).strip()[:90] or secondary
        # If caller overrode secondary but we have duration, keep duration visible
        if dur and kind in ("start", "scheduled", "active") and dur not in secondary:
            secondary = f"{dur} · {secondary}"

    png: Optional[bytes] = None
    if not skip_image:
        av = await avatar_bytes(user)
        poster = poster_bytes
        if poster is None and focus:
            try:
                poster = await fetch_poster(focus, media_filter)
            except Exception:  # noqa: BLE001
                poster = None
        png = await render_challenge_card(
            kind=kind if kind in (
                "start", "scheduled", "complete", "fail", "abandon",
                "expired", "active", "status",
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
    title_bit = f"**{title}**" if title else "a challenge"
    num_bit = f" #{int(number)}" if number else ""
    focus_bit = f" · _{focus}_" if focus else ""
    captions = {
        "start": f"🏁 {mention} started {title_bit}{focus_bit}",
        "scheduled": f"📅 {mention} scheduled {title_bit}{focus_bit}",
        "complete": f"🎉 {mention} completed {title_bit}",
        "fail": f"💀 {mention} failed a check · {title_bit}",
        "abandon": f"🏳️ {mention} abandoned {title_bit}",
        "expired": f"⏰ {mention} ran out of time · {title_bit}",
        "active": (
            f"📈 {mention} · {primary} · {title_bit}"
            if line_primary
            else f"📌 {mention} — active · {title_bit}"
        ),
        "status": f"📈 {mention} · **{primary}** · {title_bit}",
    }
    caption = captions.get(kind, f"{mention} · {kind}")
    # Status: card carries secondary (bar + honor copy); keep caption tight
    if kind == "status" and secondary and skip_image:
        caption = f"{caption}\n{secondary}"
    tmark = tier_caption_mark(tier)
    if tmark:
        caption = f"{tmark}{num_bit} · {caption}"
    when_bits: list[str] = []
    if dur and kind in ("start", "scheduled", "active", "status"):
        when_bits.append(dur)
    if kind == "scheduled" and starts_at_ts:
        when_bits.append(f"Starts {_discord_when(starts_at_ts)}")
        if ends_at_ts:
            when_bits.append(f"window ends {_discord_when(ends_at_ts, relative=False)}")
    elif kind in ("start", "active", "status") and ends_at_ts:
        when_bits.append(f"Ends {_discord_when(ends_at_ts)}")
    elif kind == "expired" and ends_at_ts:
        when_bits.append(f"Ended {_discord_when(ends_at_ts, relative=False)}")
    if when_bits:
        caption = f"{caption}\n" + " | ".join(when_bits)
    # Soft roast on fail / time-out; multi-loss tone from expired attempt count
    if kind in ("fail", "expired"):
        uid = getattr(user, "id", None)
        n = failure_count(str(uid)) if uid is not None else 0
        caption = f"{caption}\n{format_shame_caption(kind, n)}"
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
    line_primary: Optional[str] = None,
    line_secondary: Optional[str] = None,
    duration_hours: float | None = None,
    highlights_only: bool = False,
) -> bool:
    """Post lifecycle cards to configured announce channels.

    Channel routing (defaults / env):
      ANNOUNCE_CHANNEL_ID (highlights):
        start, complete, fail — congrats gif/frame on complete only here
      ANNOUNCE_SECONDARY_CHANNEL_ID (log):
        scheduled, start, active/status, complete, fail, abandon, expired, …

    New-challenger banner on start/scheduled (wherever those kinds are sent).
    highlights_only=True skips the log channel (one-off / admin test).
    """
    highlights = await resolve_announce_channel(bot, guild)
    log_ch = await resolve_secondary_announce_channel(bot, guild)
    if highlights is None and log_ch is None:
        return False

    # Resolve user object if we only have an id-ish display path
    if user is None and display_name and str(display_name).isdigit():
        try:
            user = bot.get_user(int(display_name)) or await bot.fetch_user(int(display_name))
        except (TypeError, ValueError, discord.HTTPException):
            user = None

    # Status milestones: full card with hero + time/goal bar (kind=status)
    need_card = True
    png: bytes | None = None
    caption: str | None = None
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
            line_primary=line_primary,
            line_secondary=line_secondary,
            ends_at_ts=ends_at_ts,
            starts_at_ts=starts_at_ts,
            media_filter=media_filter,
            skip_image=False,
            duration_hours=duration_hours,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("announce card build failed (%s): %s", kind, exc)
        png, caption = None, None

    who = user.mention if user is not None else (display_name or "Someone")
    focus = (content or param or "").strip()
    extra = f" · _{focus}_" if focus else ""
    text_fallback = f"{who} · **{kind}** · **{tier} #{number}: {title}**{extra}"

    cong_path = None
    if kind == "complete" and need_card:
        cong_path = pick_congrats(tier=tier, content=focus)

    # Start/schedule: NC banner + full challenge card (not the thin "Good luck" chip)
    nc_png: bytes | None = None
    if kind in ("start", "scheduled"):
        try:
            av = await avatar_bytes(user)
            nc_png = render_new_challenger(av, tier=tier)
        except Exception as exc:  # noqa: BLE001
            logger.warning("new_challenger render failed: %s", exc)
            nc_png = None

    posted = False

    async def _send(
        channel: discord.TextChannel, *,
        with_congrats: bool,
    ) -> bool:
        nonlocal posted
        try:
            body = caption or text_fallback
            files = []
            if nc_png is not None:
                files.append(as_discord_new_challenger(nc_png))
            if png is not None:
                files.append(as_discord_file(png, f"{kind}-card.png"))
                if with_congrats and cong_path is not None:
                    try:
                        files.append(
                            as_discord_framed_congrats(
                                cong_path,
                                tier=tier,
                                title=title,
                                number=int(number or 0),
                            )
                        )
                    except OSError as exc:
                        logger.warning("congrats file open failed: %s", exc)
            if not files:
                await channel.send(body)
                posted = True
                return True
            # No embed color strip — avoids double left border with PNG rails
            embeds = files_as_image_embeds(files, chrome=False)
            await channel.send(content=body, files=files, embeds=embeds)
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

    # Highlights: start + complete/fail only (congrats on complete)
    if highlights is not None and kind in _HIGHLIGHT_KINDS:
        await _send(highlights, with_congrats=(kind == "complete"))
    # Log channel: everything (no congrats media — keeps feed lighter)
    if not highlights_only and log_ch is not None and (
        highlights is None or log_ch.id != highlights.id
    ):
        await _send(log_ch, with_congrats=False)
    elif (
        not highlights_only
        and log_ch is not None
        and highlights is not None
        and log_ch.id == highlights.id
    ):
        # Same channel configured twice: already sent if highlight kind
        if kind not in _HIGHLIGHT_KINDS:
            await _send(log_ch, with_congrats=False)

    return posted


def announce_kind_for_status(status: str, *, with_challenger: bool = False) -> str:
    """Map attempt.status → announce kind.

    with_challenger=True forces start (new-challenger banner + start card)
    useful for re-skinning an active attempt with the new design pair.
    """
    st = (status or "").lower().strip()
    if with_challenger and st in ("active", "scheduled"):
        # scheduled keeps scheduled card + challenger; active uses start pair
        if st == "scheduled":
            return "scheduled"
        return "start"
    return {
        "scheduled": "scheduled",
        "active": "active",
        "completed": "complete",
        "expired": "expired",
        "abandoned": "abandon",
        "void": "abandon",
    }.get(st, "status")


async def repost_attempt_announce(
    bot: discord.Client,
    attempt: dict,
    *,
    with_challenger: bool = False,
    highlights_only: bool = False,
) -> bool:
    """Rebuild current lifecycle announce for an attempt (admin repost)."""
    from app.services.verify import parse_iso

    kind = announce_kind_for_status(
        str(attempt.get("status") or ""), with_challenger=with_challenger
    )
    user = None
    did = attempt.get("discord_id")
    try:
        if did and str(did).isdigit():
            user = bot.get_user(int(did)) or await bot.fetch_user(int(did))
    except (TypeError, ValueError, discord.HTTPException):
        user = None

    guild = None
    gid = attempt.get("guild_id")
    if not gid:
        rt = _runtime()
        gid = rt.get("guild_id") or ""
    if gid and str(gid).isdigit():
        guild = bot.get_guild(int(gid))

    def _ts(iso: str | None) -> int | None:
        if not iso:
            return None
        try:
            dt = parse_iso(str(iso))
            if dt.tzinfo is None:
                from datetime import timezone

                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:  # noqa: BLE001
            return None

    starts = _ts(attempt.get("started_at"))
    ends = _ts(attempt.get("ends_at"))
    try:
        dur_h = float(attempt.get("time_limit_hours") or 0) or None
    except (TypeError, ValueError):
        dur_h = None
    return await announce(
        bot,
        kind=kind,
        guild=guild,
        user=user,
        display_name=attempt.get("display_name") or str(did or ""),
        title=attempt.get("title") or "?",
        tier=attempt.get("tier") or "?",
        number=int(attempt.get("number") or 0),
        param=attempt.get("param"),
        content=display_content(attempt),
        method=attempt.get("verify_method"),
        ends_at_ts=ends,
        starts_at_ts=starts if kind in ("scheduled", "start", "active") else None,
        media_filter=attempt.get("media_filter"),
        duration_hours=dur_h,
        highlights_only=highlights_only,
    )


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
