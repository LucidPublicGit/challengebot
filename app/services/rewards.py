from __future__ import annotations

import logging
from typing import Any, Optional

import discord

from app.challenge_card import (
    as_discord_file,
    avatar_bytes,
    files_as_image_embeds,
    render_challenge_card,
    render_unified_start_card,
    tier_caption_mark,
)
from app.config import announce_kind_enabled, get_settings, merge_runtime
from app.congrats import as_discord_framed_congrats, pick_congrats
from app.db import db_session, get_all_settings
from app.poster import fetch_poster
from app.services.attempts import (
    completion_counts,
    display_content,
    failure_count,
    get_tz_offset,
)
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


async def send_user_only(
    bot: discord.Client,
    *,
    guild: discord.Guild | None,
    user: discord.abc.User,
    content: str,
    thread_name: str,
) -> str | None:
    """Reach one user privately without a public channel post.

    Prefers a private thread in the log/announce channel (server-side, not a DM).
    Falls back to DM if threads fail. Returns ``"thread"``, ``"dm"``, or ``None``.
    """
    body = (content or "")[:2000]
    if not body:
        return None

    member: discord.Member | None = None
    if guild is not None:
        member = guild.get_member(user.id)
        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except discord.HTTPException:
                member = None

    channels: list[discord.TextChannel] = []
    if guild is not None:
        for resolver in (
            resolve_secondary_announce_channel,
            resolve_announce_channel,
        ):
            ch = await resolver(bot, guild)
            if ch is not None and ch not in channels:
                channels.append(ch)

    if member is not None:
        for channel in channels:
            try:
                thread = await channel.create_thread(
                    name=(thread_name or "challenge")[:100],
                    type=discord.ChannelType.private_thread,
                    invitable=False,
                    auto_archive_duration=1440,
                    reason="Challenge private notice",
                )
                try:
                    await thread.add_user(member)
                except discord.HTTPException as exc:
                    logger.info(
                        "private thread add_user failed thread=%s: %s",
                        getattr(thread, "id", "?"),
                        exc,
                    )
                    # Still try send — user may already be a member somehow
                await thread.send(body)
                return "thread"
            except discord.HTTPException as exc:
                logger.info(
                    "private thread failed channel=%s: %s", channel.id, exc
                )

    try:
        dm_user = user if isinstance(user, discord.User) else await bot.fetch_user(
            user.id
        )
        await dm_user.send(body)
        return "dm"
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.info("user-only DM failed user=%s: %s", user.id, exc)
        return None


# Default highlights kinds (overridden by runtime highlight_kinds)
_HIGHLIGHT_KINDS = frozenset({"start", "complete", "fail"})


def _parse_offset_tz(offset: str):
    """Fixed UTC offset string → datetime.timezone (no DST)."""
    from datetime import timedelta, timezone
    import re

    raw = (offset or "+00:00").strip().replace(" ", "") or "+00:00"
    if raw.upper().startswith("UTC"):
        raw = raw[3:] or "+00:00"
    m = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?", raw)
    if not m:
        return timezone.utc
    sign = 1 if m.group(1) == "+" else -1
    hours = int(m.group(2))
    mins = int(m.group(3) or "0")
    return timezone(timedelta(minutes=sign * (hours * 60 + mins)))


def _fmt_short_local(ts: int | None, offset: str | None = None) -> str:
    """Short wall time in user offset: MM/DD/YY HH:MM."""
    if not ts:
        return ""
    from datetime import datetime, timezone

    tz = _parse_offset_tz(offset or "+00:00")
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(tz)
    return dt.strftime("%m/%d/%y %H:%M")


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
    include_challenger: bool | None = None,
    tz_offset: str | None = None,
) -> tuple[Optional[bytes], str]:
    """Return (png_bytes|None, short_caption).

    start/scheduled → one unified card (NC inside when include_challenger).
    Caption omitted when the card already holds the facts (mention-only ping).
    """
    name = (
        getattr(user, "display_name", None)
        or display_name
        or (str(user) if user else "Someone")
    )
    focus = (content or param or "").strip()
    from app.services.verify import parse_content_items

    focus_items = parse_content_items(focus)
    dur = _duration_from_window(duration_hours, starts_at_ts, ends_at_ts)

    # Resolve wall-clock offset for short local dates on start/scheduled cards
    off = (tz_offset or "").strip()
    if not off and user is not None and getattr(user, "id", None) is not None:
        try:
            off = get_tz_offset(str(user.id))
        except Exception:  # noqa: BLE001
            off = "+00:00"
    if not off:
        off = "+00:00"

    # --- Unified start / scheduled (one box; no "Started" hero) ---
    if kind in ("start", "scheduled") and not skip_image:
        with_nc = (
            True
            if kind == "scheduled"
            else (True if include_challenger is None else bool(include_challenger))
        )
        # Honor announce_new_challenger kill-switch
        if with_nc and not _runtime().get("announce_new_challenger", True):
            with_nc = False

        schedule_label = None
        meta_line = ""
        if kind == "scheduled":
            when = _fmt_short_local(starts_at_ts, off) or _fmt_utc(starts_at_ts)
            schedule_label = f"Scheduled for {when}" if when else "Scheduled"
            if dur:
                meta_line = dur  # still useful under the pill? keep off card body via pill only
                # duration lives on schedule caption area as secondary bit in pill path:
                # append to schedule label if short
                if dur and when:
                    schedule_label = f"Scheduled for {when} · {dur}"
        else:
            ends_s = _fmt_short_local(ends_at_ts, off)
            bits = [b for b in (dur, f"Ends {ends_s}" if ends_s else "") if b]
            meta_line = " · ".join(bits) or "Window open"

        focus_one = focus_items[0] if focus_items else (focus.split("\n")[0].strip() if focus else "")
        poster = poster_bytes
        if poster is None and focus_one:
            try:
                poster = await fetch_poster(focus_one, media_filter)
            except Exception:  # noqa: BLE001
                poster = None
        av = await avatar_bytes(user)
        try:
            png = await render_unified_start_card(
                display_name=name,
                challenge_title=title,
                tier=tier,
                number=int(number or 0),
                avatar_bytes=av,
                focus=focus_one if len(focus_items) <= 1 else ", ".join(focus_items[:4]),
                meta_line=meta_line if kind == "start" else "",
                schedule_label=schedule_label,
                include_challenger=with_nc,
                poster_bytes=poster,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("unified start card failed: %s", exc)
            png = None

        # Card already has name/title/focus/times — mention only (ping), else empty
        caption = user.mention if user is not None else ""
        return png, caption

    primary = ""
    secondary = ""
    footer = ""
    if kind == "start":
        # skip_image path / fallback text only
        primary = "Good luck"
        ends_s = _fmt_short_local(ends_at_ts, off)
        bits = [b for b in (dur, f"Ends {ends_s}" if ends_s else _ts_line("Ends", ends_at_ts)) if b]
        secondary = " · ".join(bits) or "Window open"
    elif kind == "scheduled":
        primary = "Queued to start"
        when = _fmt_short_local(starts_at_ts, off) or _fmt_utc(starts_at_ts)
        bits = [b for b in (dur, f"Scheduled for {when}" if when else "") if b]
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
        # Cover search uses first work only — never the whole multi-line blob
        cover_q = focus_items[0] if focus_items else (focus.split("\n")[0].strip() if focus else "")
        if poster is None and cover_q:
            try:
                poster = await fetch_poster(cover_q, media_filter)
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

    def _focus_caption() -> str:
        items = focus_items or ([focus] if focus else [])
        if not items:
            return ""
        if len(items) == 1:
            return f" · _{items[0]}_"
        return "\n" + "\n".join(f"• {it}" for it in items)

    focus_bit = _focus_caption()
    captions = {
        "start": f"🏁 {mention} started {title_bit}{focus_bit}",
        "scheduled": f"📅 {mention} scheduled {title_bit}{focus_bit}",
        "complete": f"🎉 {mention} completed {title_bit}{focus_bit}",
        "fail": f"💀 {mention} failed a check · {title_bit}{focus_bit}",
        "abandon": f"🏳️ {mention} abandoned {title_bit}{focus_bit}",
        "expired": f"⏰ {mention} ran out of time · {title_bit}{focus_bit}",
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
    if kind in ("fail", "expired") and _runtime().get("announce_shame", True):
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
    include_challenger: bool | None = None,
) -> bool:
    """Post lifecycle cards to configured announce channels.

    Channel routing (defaults / env):
      ANNOUNCE_CHANNEL_ID (highlights):
        start, complete, fail — congrats gif/frame on complete only here
      ANNOUNCE_SECONDARY_CHANNEL_ID (log):
        scheduled, start, complete, fail, abandon, expired, …

    start/scheduled: one unified PNG (NC embedded when include_challenger).
    Go-live after schedule: kind=start + include_challenger=False (no NC).
    highlights_only=True skips the log channel (one-off / admin test).
    """
    rt = _runtime()
    if not announce_kind_enabled(rt, kind):
        return False

    highlights = await resolve_announce_channel(bot, guild)
    log_ch = await resolve_secondary_announce_channel(bot, guild)
    if highlights is None and log_ch is None:
        return False

    highlight_kinds = frozenset(rt.get("highlight_kinds") or _HIGHLIGHT_KINDS)

    # Resolve user object if we only have an id-ish display path
    if user is None and display_name and str(display_name).isdigit():
        try:
            user = bot.get_user(int(display_name)) or await bot.fetch_user(int(display_name))
        except (TypeError, ValueError, discord.HTTPException):
            user = None

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
            include_challenger=include_challenger,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("announce card build failed (%s): %s", kind, exc)
        png, caption = None, None

    who = user.mention if user is not None else (display_name or "Someone")
    focus = (content or param or "").strip()
    from app.services.verify import parse_content_items as _pci

    _items = _pci(focus)
    if len(_items) > 1:
        extra = "\n" + "\n".join(f"• {it}" for it in _items)
    elif _items:
        extra = f" · _{_items[0]}_"
    elif focus:
        extra = f" · _{focus.splitlines()[0].strip()}_"
    else:
        extra = ""
    text_fallback = f"{who} · **{kind}** · **{tier} #{number}: {title}**{extra}"

    # start/scheduled: card is self-contained — keep caption only if it adds info
    # (mention ping is the only extra; empty caption → image-only post)
    if kind in ("start", "scheduled"):
        body_default = (caption or "").strip() or None
    else:
        body_default = (caption or text_fallback).strip() or text_fallback

    cong_path = None
    if kind == "complete" and rt.get("announce_congrats", True):
        # Congrats match on first work title, not the whole multi-line blob
        cong_focus = _items[0] if _items else focus
        try:
            cong_path = pick_congrats(tier=tier, content=cong_focus)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pick_congrats failed: %s", exc)
            cong_path = None
        if cong_path is None:
            logger.warning(
                "No congrats image found (tier=%s content=%r). "
                "Check completion_pictures/ mounts.",
                tier,
                (cong_focus or "")[:80],
            )

    posted = False

    def _build_files(*, with_congrats: bool) -> list:
        """Fresh discord.File objects (single-use streams)."""
        files = []
        # NC is composited into the unified start/scheduled PNG — no second file
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
            except Exception as exc:  # noqa: BLE001
                logger.warning("congrats attach failed (%s): %s", cong_path, exc)
        return files

    async def _send(
        channel: discord.TextChannel, *,
        with_congrats: bool,
    ) -> bool:
        nonlocal posted
        body = body_default

        async def _try_send(files: list, *, content: str | None) -> bool:
            """One embed per file so Discord shows images full-size (one box)."""
            if not files and not content:
                return False
            if not files:
                await channel.send(content)
                return True
            embeds = files_as_image_embeds(files, chrome=False)
            kw: dict = {"files": files, "embeds": embeds}
            if content:
                kw["content"] = content
            await channel.send(**kw)
            return True

        # 1) card (+ optional congrats) together
        try:
            if await _try_send(_build_files(with_congrats=with_congrats), content=body):
                posted = True
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "announce multi-file failed #%s (%s): %s — splitting",
                getattr(channel, "id", "?"),
                kind,
                exc,
            )

        # 2) card, then congrats as its own embedded message
        try:
            await _try_send(_build_files(with_congrats=False), content=body)
            if with_congrats and cong_path is not None:
                await _try_send(_build_files(with_congrats=True)[-1:], content=None)
            posted = True
            return True
        except Exception as exc2:  # noqa: BLE001
            logger.warning(
                "announce split failed #%s: %s",
                getattr(channel, "id", "?"),
                exc2,
            )

        try:
            await channel.send(text_fallback)
            if with_congrats and cong_path is not None:
                try:
                    await _try_send(_build_files(with_congrats=True)[-1:], content=None)
                except Exception as exc3:  # noqa: BLE001
                    logger.warning("congrats-only send failed: %s", exc3)
            posted = True
            return True
        except discord.HTTPException as exc4:
            logger.warning("announce text fallback failed: %s", exc4)
            return False

    want_congrats = kind == "complete" and cong_path is not None
    # Highlights: configured kinds (default start + complete/fail)
    if highlights is not None and kind in highlight_kinds:
        await _send(highlights, with_congrats=want_congrats)
    # Log channel: everything; also attach congrats on complete so it isn't missed
    if not highlights_only and log_ch is not None and (
        highlights is None or log_ch.id != highlights.id
    ):
        await _send(log_ch, with_congrats=want_congrats)
    elif (
        not highlights_only
        and log_ch is not None
        and highlights is not None
        and log_ch.id == highlights.id
    ):
        # Same channel configured twice: already sent if highlight kind
        if kind not in highlight_kinds:
            await _send(log_ch, with_congrats=want_congrats)

    return posted


def announce_kind_for_status(status: str, *, with_challenger: bool = False) -> str:
    """Map attempt.status → announce kind.

    with_challenger=True forces start/scheduled unified card (NC on start).
    """
    st = (status or "").lower().strip()
    if with_challenger and st in ("active", "scheduled"):
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
    # Active + with_challenger → immediate-start (NC). Scheduled always NC.
    inc_nc: bool | None = None
    if kind == "start":
        inc_nc = True if with_challenger else False
    elif kind == "scheduled":
        inc_nc = True
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
        include_challenger=inc_nc,
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
