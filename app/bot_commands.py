"""Slash commands + Discord UI (buttons / selects / modals)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands

from app.catalog import get_challenge, list_challenges, needs_focus_param
from app.challenge_card import (
    as_discord_file,
    avatar_bytes,
    files_as_image_embeds,
    render_all_demo_cards,
    render_leaderboard_card,
    render_userinfo_card,
)
from app.congrats import as_discord_framed_congrats, pick_congrats
from app.db import db_session
from app.new_challenger import list_templates
from app.services import attempts as att_svc
from app.services import progress_status as prog_svc
from app.services.rewards import (
    announce,
    announce_completion,
    apply_rewards,
    build_event_card,
)

logger = logging.getLogger(__name__)


def _cleared_note() -> str:
    if att_svc.reattempts_allowed():
        return "✅ = already cleared."
    return "✅ = cleared (one-time — can't start again)."


def _challenge_done_tag(challenge_id: str, done_ids: set[str]) -> str:
    if challenge_id not in done_ids:
        return ""
    if att_svc.reattempts_allowed():
        return " ✅ cleared"
    return " ✅ cleared · one-time only"


# Imported lazily-friendly bits from bot to avoid cycles at import time
def _bot_helpers():
    from app import bot as b

    return b


async def _layout_e_start_files(
    *,
    user: discord.abc.User | None,
    kind: str,
    title: str,
    tier: str,
    number: int = 0,
    focus: str | None = None,
    ends_at_ts: int | None = None,
    starts_at_ts: int | None = None,
    media_filter: str | None = None,
    card_png: bytes | None = None,
    duration_hours: float | None = None,
    include_challenger: bool | None = None,
) -> list:
    """One unified start/scheduled card (NC composited when include_challenger)."""
    png = card_png
    if png is None:
        png, _ = await build_event_card(
            kind=kind if kind in ("start", "scheduled") else "start",
            user=user,
            title=title,
            tier=tier,
            number=int(number or 0),
            param=focus,
            content=focus,
            ends_at_ts=ends_at_ts,
            starts_at_ts=starts_at_ts,
            media_filter=media_filter,
            duration_hours=duration_hours,
            include_challenger=include_challenger,
        )
    if png:
        return [as_discord_file(png, f"{kind}-card.png")]
    return []


challenge_group = app_commands.Group(
    name="challenge",
    description="Yearly immersion challenges",
)

TIER_META = (
    ("easy", "Easy", discord.ButtonStyle.success),
    ("medium", "Medium", discord.ButtonStyle.primary),
    ("hard", "Hard", discord.ButtonStyle.danger),
    ("ota_master", "Ota-Master", discord.ButtonStyle.secondary),
)

TIER_CHOICES = [
    app_commands.Choice(name="Easy", value="easy"),
    app_commands.Choice(name="Medium", value="medium"),
    app_commands.Choice(name="Hard", value="hard"),
    app_commands.Choice(name="Ota-Master", value="ota_master"),
]

# Fixed offsets (no auto-DST). Pick what your clock shows *right now*.
# label, value, optional select description (Discord shows under the label).
OFFSET_OPTIONS: list[tuple[str, str, str]] = [
    ("UTC-10 · Hawaii", "-10:00", "Honolulu year-round"),
    ("UTC-8 · US Pacific", "-08:00", "LA / Seattle in winter (PST)"),
    ("UTC-7 · US Pacific summer", "-07:00", "LA / Seattle in summer (PDT); Denver winter"),
    ("UTC-6 · US Mountain / Central", "-06:00", "Denver summer (MDT); Chicago winter (CST)"),
    ("UTC-5 · US Central / Eastern", "-05:00", "Chicago summer (CDT); New York winter (EST)"),
    ("UTC-4 · US Eastern summer", "-04:00", "New York / Toronto in summer (EDT)"),
    ("UTC+0 · GMT", "+00:00", "London in winter; Iceland year-round"),
    ("UTC+1 · UK / Central Europe", "+01:00", "London summer (BST); Paris winter (CET)"),
    ("UTC+2 · Central Europe summer", "+02:00", "Paris / Berlin in summer (CEST)"),
    ("UTC+5:30 · India", "+05:30", "India / Sri Lanka year-round"),
    ("UTC+8 · China / Singapore", "+08:00", "Beijing, Singapore, Perth year-round"),
    ("UTC+9 · Japan / Korea", "+09:00", "Tokyo, Seoul year-round"),
    ("UTC+10 · Eastern Australia", "+10:00", "Sydney / Melbourne in winter (AEST)"),
]

_VALID_OFFSETS = {v for _, v, _ in OFFSET_OPTIONS}


def _catalog_year() -> int:
    items = list_challenges()
    return max((int(c["year"]) for c in items), default=2026)


def _start_embed(
    *,
    title: str = "Start a challenge",
    description: str,
    ch: dict | None = None,
) -> discord.Embed:
    b = _bot_helpers()
    embed = discord.Embed(title=title, description=description, color=0x5865F2)
    if ch:
        embed.add_field(
            name="Goal",
            value=(
                f"`{ch.get('goal_kind')}` {ch.get('goal_amount')} · "
                f"media `{ch.get('media_filter') or 'any'}` · "
                f"confirm **{b.verify_label(ch)}**"
            ),
            inline=False,
        )
        if ch.get("description"):
            embed.add_field(name="Details", value=ch["description"][:300], inline=False)
    embed.set_footer(text="Only you can use these controls · times out in 10 min")
    return embed


def _user_tadoku_linked(discord_id: int | str) -> bool:
    user = att_svc.get_user(str(discord_id))
    return bool(user and user.get("tadoku_user_id"))


def _challenge_needs_tadoku(ch: dict | None) -> bool:
    if not ch:
        return False
    return (ch.get("verify") or "tadoku").lower() == "tadoku"


def _release_view(view: discord.ui.View | None) -> discord.Message | None:
    """Stop a wizard step and detach its message so timeout can't stomp the next step."""
    if view is None:
        return None
    msg = getattr(view, "message", None)
    if hasattr(view, "message"):
        view.message = None  # type: ignore[attr-defined]
    view.stop()
    return msg


def _bind_view_message(
    view: discord.ui.View, message: discord.Message | None
) -> None:
    if hasattr(view, "message"):
        view.message = message  # type: ignore[attr-defined]


def _when_pick_for(
    owner_id: int,
    *,
    ch: dict,
    focus: str | None,
    guild_id: str | None,
    year: int | None = None,
    tier: str | None = None,
    dry_run: bool = False,
) -> "WhenPickView":
    return WhenPickView(
        owner_id,
        ch=ch,
        focus=focus,
        year=int(year if year is not None else ch.get("year") or _catalog_year()),
        guild_id=guild_id,
        tier=str(tier if tier is not None else ch.get("tier") or ""),
        dry_run=dry_run,
    )


async def _finalize_start(
    interaction: discord.Interaction,
    *,
    challenge_id: str,
    focus: str | None,
    guild_id: str | None,
    starts_at: datetime | None = None,
    restore_view: discord.ui.View | None = None,
) -> None:
    b = _bot_helpers()
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    result = att_svc.start_attempt(
        str(interaction.user.id),
        challenge_id=challenge_id,
        param=focus,
        guild_id=guild_id,
        display_name=interaction.user.display_name,
        starts_at=starts_at,
    )
    if not result.get("ok"):
        err = result.get("error") or "Failed."
        ch = get_challenge(challenge_id)
        # Restore a live panel so "Starting…" with dead buttons is not a dead end.
        try:
            if ch is not None:
                restored = _when_pick_for(
                    interaction.user.id,
                    ch=ch,
                    focus=focus,
                    guild_id=guild_id,
                )
                emb = restored._when_embed()
                emb.description = (
                    f"**Couldn’t start:** {err}\n\n" + (emb.description or "")
                )
                await interaction.edit_original_response(embed=emb, view=restored)
                _release_view(restore_view)
                _bind_view_message(
                    restored, await interaction.original_response()
                )
            elif restore_view is not None:
                for child in restore_view.children:
                    child.disabled = False  # type: ignore[attr-defined]
                await interaction.edit_original_response(view=restore_view)
        except discord.HTTPException:
            logger.warning("could not restore start panel after failed start")
            try:
                await interaction.followup.send(err, ephemeral=True)
            except discord.HTTPException:
                pass
            return
        return

    ch = result["challenge"]
    ends = b._ts(result["ends_at"])
    start_ts = b._ts(result["started_at"])
    status = result.get("status") or "active"
    kind = "scheduled" if status == "scheduled" else "start"
    media = ch.get("media_filter")
    focus = (result.get("param") or focus or "").strip() or None
    try:
        dur_h = float(ch.get("time_limit_hours") or 0) or None
    except (TypeError, ValueError):
        dur_h = None
    png, caption = await build_event_card(
        kind=kind,
        user=interaction.user,
        title=ch["title"],
        tier=ch["tier"],
        number=int(ch["number"]),
        param=focus,
        content=focus,
        starts_at_ts=start_ts if kind == "scheduled" else None,
        ends_at_ts=ends,
        media_filter=media,
        duration_hours=dur_h,
    )
    aid = result.get("attempt_id") or (result.get("attempt") or {}).get("id")
    try:
        aid_i = int(aid) if aid is not None else None
    except (TypeError, ValueError):
        aid_i = None
    if kind == "scheduled":
        howto = (
            "**Scheduled** — public card when it starts; you'll get a private "
            "how-to thread/DM at go-live.\n"
            "Cancel anytime: `/challenge abandon`"
            + (f" `attempt_id:{aid_i}`" if aid_i is not None else ".")
        )
    else:
        howto = b.confirm_howto(ch, attempt_id=aid_i)
    files = await _layout_e_start_files(
        user=interaction.user,
        kind=kind,
        title=ch["title"],
        tier=ch["tier"],
        number=int(ch["number"]),
        focus=focus,
        ends_at_ts=ends,
        starts_at_ts=start_ts if kind == "scheduled" else None,
        media_filter=media,
        card_png=png,
        duration_hours=dur_h,
    )
    # Ephemeral = only the starter sees complete/update instructions (not a DM)
    guide = (
        f"**Only you see this guide**\n{caption}\n\n{howto}"
    )[:1900]
    kwargs: dict = {"content": guide, "ephemeral": True}
    if files:
        kwargs["files"] = files
        kwargs["embeds"] = files_as_image_embeds(files, chrome=False)
    await interaction.followup.send(**kwargs)
    # Channel card immediately (schedule or start); start-at-time is expire_loop
    await announce(
        interaction.client,
        kind=kind,
        guild=interaction.guild,
        user=interaction.user,
        title=ch["title"],
        tier=ch["tier"],
        number=int(ch["number"]),
        param=focus,
        content=focus,
        starts_at_ts=start_ts if kind == "scheduled" else None,
        ends_at_ts=ends,
        media_filter=media,
        duration_hours=dur_h,
    )


async def _run_confirm(
    interaction: discord.Interaction,
    *,
    note: str | None = None,
    content: str | None = None,
    filter_mode: str | None = None,
    exclude: str | None = None,
    aliases: str | None = None,
    log_ids: str | None = None,
    attempt_id: int | None = None,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    ignore_param = False
    param_override = None
    honor_override = False
    fm = (filter_mode or "").strip()
    if fm:
        low = fm.lower()
        if low in ("all", "*", "-", "any", "none", "clear"):
            ignore_param = True
        elif low in ("honor", "honour", "manual", "override"):
            honor_override = True
        else:
            param_override = fm
    result = await att_svc.confirm_attempt(
        str(interaction.user.id),
        note=note,
        content=content,
        ignore_param=ignore_param,
        param_override=param_override,
        exclude=exclude,
        aliases=aliases,
        log_ids=log_ids,
        honor_override=honor_override,
        attempt_id=attempt_id,
    )
    if not result.get("ok"):
        msg = result.get("error") or "Not completed."
        if result.get("progress"):
            msg += f"\nProgress: {result['progress']}"
        if result.get("log_count") is not None:
            msg += f"\nLogs seen in window: {result['log_count']}"
        att = result.get("attempt")
        aid = int(att["id"]) if att and att.get("id") is not None else attempt_id
        # Always point at hard user backdoor when override is allowed
        from app.config import get_settings, merge_runtime
        from app.db import db_session, get_all_settings

        with db_session() as conn:
            rt = merge_runtime(get_settings(), get_all_settings(conn))
        view = None
        if rt.get("allow_honor_override", True) and aid is not None:
            msg += (
                "\n\n**Mistake / Tadoku miss?** "
                f"`/challenge failoverride` (attempt `{aid}`) — "
                "honor complete without Tadoku (no admin needed)."
            )
            hint = content
            if att:
                hint = content or att_svc.display_content(att)
            view = FailOverridePromptView(
                interaction.user.id,
                attempt_id=int(aid),
                content_hint=hint,
            )
        kw: dict = {"content": msg[:1900], "ephemeral": True}
        if view:
            kw["view"] = view
        await interaction.followup.send(**kw)
        if att and result.get("verify") is not None:
            await announce(
                interaction.client,
                kind="fail",
                guild=interaction.guild,
                user=interaction.user,
                title=att.get("title") or "?",
                tier=att.get("tier") or "?",
                number=int(att.get("number") or 0),
                param=att.get("param"),
                content=att_svc.display_content(att),
                detail=result.get("progress")
                or (result.get("verify") or {}).get("detail"),
                media_filter=att.get("media_filter"),
            )
        return

    att = result["attempt"]
    method = result.get("method") or "tadoku"
    content = result.get("content") or att_svc.display_content(att)
    detail = (result.get("verify") or {}).get("detail") if result.get("verify") else None
    roles: list[str] = []
    if interaction.guild and isinstance(interaction.user, discord.Member):
        roles = await apply_rewards(interaction.user, tier=att["tier"])
    await announce(
        interaction.client,
        kind="complete",
        guild=interaction.guild,
        user=interaction.user,
        title=att["title"],
        tier=att["tier"],
        number=int(att["number"]),
        param=att.get("param"),
        content=content,
        method=method,
        detail=detail,
        media_filter=att.get("media_filter"),
    )
    png, caption = await build_event_card(
        kind="complete",
        user=interaction.user,
        title=att["title"],
        tier=att["tier"],
        number=int(att["number"]),
        param=att.get("param"),
        content=content,
        method=method,
        detail=detail,
        media_filter=att.get("media_filter"),
    )
    extra = f"\nRoles: {', '.join(roles)}" if roles else ""
    card = as_discord_file(png, "complete-card.png")
    await interaction.followup.send(
        content=f"{caption}{extra}",
        file=card,
        embeds=files_as_image_embeds([card], kind="complete"),
        ephemeral=True,
    )


async def _run_abandon(
    interaction: discord.Interaction, *, attempt_id: int | None = None
) -> None:
    result = att_svc.abandon_attempt(
        str(interaction.user.id), attempt_id=attempt_id
    )
    if not result.get("ok"):
        msg = result.get("error") or "Nope"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return
    att = result["attempt"]
    msg = "Abandoned."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    await announce(
        interaction.client,
        kind="abandon",
        guild=interaction.guild,
        user=interaction.user,
        title=att.get("title") or "?",
        tier=att.get("tier") or "?",
        number=int(att.get("number") or 0),
        param=att.get("param"),
        content=att_svc.display_content(att),
        media_filter=att.get("media_filter"),
    )


class _OwnerView(discord.ui.View):
    # Start wizard is multi-step; short timeouts left dead panels after link/advance.
    def __init__(self, owner_id: int, *, timeout: float = 600) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This menu isn’t yours.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        # Only touch the message if we still own it (handoff clears this).
        msg = self.message
        self.message = None
        if msg is None:
            return
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        try:
            await msg.edit(view=self)
        except discord.HTTPException:
            pass


# ----- start wizard -----


class FocusModal(discord.ui.Modal):
    def __init__(
        self,
        ch: dict,
        *,
        owner_id: int,
        year: int,
        guild_id: str | None,
        tier: str,
    ) -> None:
        label = (ch.get("param_label") or "Focus")[:45]
        super().__init__(title=f"{ch['title']}"[:45])
        self.ch = ch
        self.owner_id = owner_id
        self.year = year
        self.guild_id = guild_id
        self.tier = tier
        self.focus_input = discord.ui.TextInput(
            label=label,
            placeholder=f"Your {label.lower()}…",
            required=True,
            max_length=100,
        )
        self.add_item(self.focus_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        focus = str(self.focus_input.value).strip()
        view = WhenPickView(
            self.owner_id,
            ch=self.ch,
            focus=focus,
            year=self.year,
            guild_id=self.guild_id,
            tier=self.tier,
        )
        emb = view._when_embed()
        if focus:
            emb.description = (
                f"Focus: **{focus}**\n\n" + (emb.description or "")
            )
        await interaction.response.send_message(
            embed=emb,
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()


def _offset_to_tz(offset: str) -> timezone:
    m = re.match(
        r"^(?:UTC)?\s*(?P<sign>[+-])\s*(?P<h>\d{1,2})(?::?(?P<m>\d{2}))?$",
        (offset or "+00:00").strip().replace(" ", ""),
        re.I,
    )
    if not m:
        raise ValueError("Bad offset")
    sign, oh, om = m.group("sign"), int(m.group("h")), int(m.group("m") or 0)
    delta = timedelta(hours=oh, minutes=om)
    if sign == "-":
        delta = -delta
    return timezone(delta)


def _normalize_offset(offset: str | None) -> str:
    off = (offset or "+00:00").strip() or "+00:00"
    if off not in _VALID_OFFSETS:
        # Accept free-form +h / -h still used in tests / old rows
        try:
            _offset_to_tz(off)
        except ValueError:
            return "+00:00"
    return off


def _local_calendar_day(offset: str, day_rel: int = 0):
    local_tz = _offset_to_tz(offset)
    base = datetime.now(timezone.utc).astimezone(local_tz)
    return (base + timedelta(days=int(day_rel))).date(), local_tz, base


def _resolve_schedule_from_picks(
    offset: str, day_rel: int, hhmm: str
) -> datetime:
    """day_rel=0 today in that offset, 1=tomorrow… + HH:MM local → UTC."""
    target_date, local_tz, _ = _local_calendar_day(offset, day_rel)
    parts = hhmm.split(":")
    h, mi = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    local_dt = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        h,
        mi,
        tzinfo=local_tz,
    )
    return local_dt.astimezone(timezone.utc).replace(microsecond=0)


def _day_label(offset: str, day_rel: int) -> str:
    d, _, _ = _local_calendar_day(offset, day_rel)
    cal = f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"
    if day_rel == 0:
        return f"Today ({cal})"
    if day_rel == 1:
        return f"Tomorrow ({cal})"
    return cal


def _days_left_in_month(offset: str) -> int:
    """Inclusive count: today through last day of the local month."""
    import calendar

    d, _, _ = _local_calendar_day(offset, 0)
    last = calendar.monthrange(d.year, d.month)[1]
    return max(1, last - d.day + 1)


def _day_select_options(
    offset: str, selected_rel: int = 0
) -> list[discord.SelectOption]:
    """Today → rest of local month (Discord max 25 options)."""
    n = min(_days_left_in_month(offset), 25)
    try:
        sel = int(selected_rel)
    except (TypeError, ValueError):
        sel = 0
    sel = max(0, min(sel, n - 1))
    opts: list[discord.SelectOption] = []
    for i in range(n):
        opts.append(
            discord.SelectOption(
                label=_day_label(offset, i)[:100],
                value=str(i),
                default=i == sel,
            )
        )
    return opts


def _clock12_label(hour24: int) -> str:
    """0 → 12 AM, 13 → 1 PM, …"""
    h12 = hour24 % 12 or 12
    ap = "AM" if hour24 < 12 else "PM"
    return f"{h12} {ap}"


def _format_wall_time(hour24: int, minute: int) -> str:
    h12 = hour24 % 12 or 12
    ap = "AM" if hour24 < 12 else "PM"
    return f"{h12}:{minute:02d} {ap}"


def _schedule_when_text(
    offset: str,
    day_rel: int,
    hhmm: str,
    *,
    day_only: bool = False,
) -> str:
    try:
        starts = _resolve_schedule_from_picks(offset, day_rel, hhmm)
    except ValueError:
        return "Could not parse that offset."
    ts = int(starts.timestamp())
    now = datetime.now(timezone.utc)
    day = _day_label(offset, day_rel)
    if day_only:
        if starts <= now and int(day_rel) == 0:
            return (
                f"**{day}** — local midnight already passed.\n"
                "Confirm will **start immediately** (still counts as today)."
            )
        return (
            f"**{day}** only — starts at **local midnight** (`{offset}`)\n"
            f"Discord: <t:{ts}:F> (<t:{ts}:R>)"
        )
    wall = hhmm
    try:
        parts = hhmm.split(":")
        wall = _format_wall_time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (TypeError, ValueError):
        pass
    if starts <= now:
        return (
            f"That is in the past: <t:{ts}:F>\n"
            f"Your pick: **{day}** at **{wall}** (`{offset}`). Choose later."
        )
    return (
        f"**{day}** at **{wall}** (`{offset}`)\n"
        f"Discord: <t:{ts}:F> (<t:{ts}:R>)"
    )


async def _commit_scheduled_start(
    interaction: discord.Interaction,
    *,
    view: discord.ui.View,
    ch: dict,
    focus: str | None,
    guild_id: str | None,
    offset: str,
    day_rel: int,
    hhmm: str,
    dry_run: bool = False,
    day_only: bool = False,
) -> None:
    offset = _normalize_offset(offset)
    try:
        starts = _resolve_schedule_from_picks(offset, day_rel, hhmm)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    now = datetime.now(timezone.utc)
    started_now = False
    if starts <= now:
        if day_only and int(day_rel) == 0:
            starts = now.replace(microsecond=0)
            started_now = True
        else:
            await interaction.response.send_message(
                "That time is already past - pick a later day/time.",
                ephemeral=True,
            )
            return
    # Remember TZ so next schedule defaults correctly
    att_svc.set_tz_offset(
        str(interaction.user.id),
        offset,
        interaction.user.display_name,
    )
    for child in view.children:
        child.disabled = True  # type: ignore[attr-defined]
    ts = int(starts.timestamp())
    day = _day_label(offset, day_rel)
    if dry_run:
        extra = (
            "Would **start immediately** (today midnight already passed).\n"
            if started_now
            else ""
        )
        mode = "day only (local midnight)" if day_only else f"exact `{hhmm}`"
        await interaction.response.edit_message(
            embed=_start_embed(
                title="TEST schedule — nothing saved",
                description=(
                    f"**{ch['title']}**\n"
                    f"{extra}"
                    f"Would start <t:{ts}:F> (<t:{ts}:R>)\n"
                    f"{day} · {mode} · offset `{offset}`\n\n"
                    "Timezone saved for next time. No attempt created."
                ),
                ch=ch,
            ),
            view=None,
        )
        return
    await interaction.response.edit_message(
        embed=_start_embed(
            title="Scheduling..." if not started_now else "Starting...",
            description=f"**{ch['title']}**\n<t:{ts}:F>",
            ch=ch,
        ),
        view=view,
    )
    await _finalize_start(
        interaction,
        challenge_id=ch["id"],
        focus=focus,
        guild_id=guild_id,
        starts_at=None if started_now else starts,
        restore_view=view,
    )


def _default_offset_for(user_id: int) -> str:
    return _normalize_offset(att_svc.get_tz_offset(str(user_id)))


def _offset_options(selected: str) -> list[discord.SelectOption]:
    selected = _normalize_offset(selected)
    seen: set[str] = set()
    opts: list[discord.SelectOption] = []
    for label, val, desc in OFFSET_OPTIONS:
        if val in seen:
            continue
        seen.add(val)
        opts.append(
            discord.SelectOption(
                label=label[:100],
                value=val,
                description=(desc or "")[:100] or None,
                default=val == selected,
            )
        )
    if selected not in seen:
        opts.insert(
            0,
            discord.SelectOption(
                label=f"Saved {selected}"[:100],
                value=selected,
                description="Previously saved offset",
                default=True,
            ),
        )
    # Discord allows only one default; ensure exactly one
    defaults = [o for o in opts if o.default]
    if len(defaults) != 1:
        for o in opts:
            o.default = o.value == selected
        if not any(o.default for o in opts) and opts:
            opts[0].default = True
    return opts[:25]


class ScheduleView(_OwnerView):
    """Schedule UI: day by default; Exact time toggles hour/minute fields."""

    def __init__(
        self,
        owner_id: int,
        *,
        ch: dict,
        focus: str | None,
        guild_id: str | None,
        year: int,
        tier: str,
        offset: str | None = None,
        day_rel: int = 0,
        hour: int = 18,
        minute: int = 0,
        exact: bool = False,
        dry_run: bool = False,
    ) -> None:
        super().__init__(owner_id)
        self.ch = ch
        self.focus = focus
        self.guild_id = guild_id
        self.year = year
        self.tier = tier
        self.dry_run = dry_run
        self.exact = bool(exact)
        self.offset = _normalize_offset(
            offset if offset is not None else _default_offset_for(owner_id)
        )
        self.day_rel = int(day_rel)
        self.hour = max(0, min(23, int(hour)))
        self.minute = int(minute) if int(minute) in (0, 15, 30, 45) else 0
        self._clamp_day_rel()
        self._build_items()

    def _clamp_day_rel(self) -> None:
        n = min(_days_left_in_month(self.offset), 25)
        self.day_rel = max(0, min(int(self.day_rel), n - 1))

    def _hhmm(self) -> str:
        if not self.exact:
            return "00:00"
        return f"{self.hour:02d}:{self.minute:02d}"

    def _dry_banner(self) -> str:
        return (
            "**TEST MODE — nothing will be saved.**\n"
            if self.dry_run
            else ""
        )

    def _schedule_embed(self) -> discord.Embed:
        when = _schedule_when_text(
            self.offset,
            self.day_rel,
            self._hhmm(),
            day_only=not self.exact,
        )
        if self.exact:
            hint = (
                "Exact time on — pick hour (12 AM–11 PM) and minutes.\n"
                "**Day only** hides the clock and uses local midnight."
            )
            title = "Schedule — exact time"
        else:
            left = _days_left_in_month(self.offset)
            cap = (
                f" (next {min(left, 25)} days; Discord max 25)"
                if left > 25
                else f" through end of month ({left} days)"
            )
            hint = (
                f"**Day** mode — starts at local midnight that day{cap}.\n"
                "Need a clock time? Press **Exact time**."
            )
            title = "Schedule — pick a day"
        return _start_embed(
            title=("TEST · " if self.dry_run else "") + title,
            description=(
                f"{self._dry_banner()}"
                f"**{self.ch['title']}**\n{when}\n\n"
                f"{hint}"
            ),
            ch=self.ch,
        )

    def _build_items(self) -> None:
        self.clear_items()
        # Timezone
        off_sel = discord.ui.Select(
            placeholder="Your timezone (UTC offset now)",
            options=_offset_options(self.offset),
            row=0,
        )

        async def on_off(interaction: discord.Interaction) -> None:
            self.offset = _normalize_offset(off_sel.values[0])
            att_svc.set_tz_offset(
                str(self.owner_id),
                self.offset,
                interaction.user.display_name,
            )
            self._clamp_day_rel()
            await self._refresh(interaction)

        off_sel.callback = on_off  # type: ignore[method-assign]
        self.add_item(off_sel)

        # Day
        day_sel = discord.ui.Select(
            placeholder="Which day?",
            options=_day_select_options(self.offset, self.day_rel),
            row=1,
        )

        async def on_day(interaction: discord.Interaction) -> None:
            self.day_rel = int(day_sel.values[0])
            await self._refresh(interaction)

        day_sel.callback = on_day  # type: ignore[method-assign]
        self.add_item(day_sel)

        btn_row = 2
        if self.exact:
            hour_sel = discord.ui.Select(
                placeholder="Time (12-hour)",
                options=[
                    discord.SelectOption(
                        label=_clock12_label(h),
                        value=str(h),
                        default=h == self.hour,
                    )
                    for h in range(24)
                ],
                row=2,
            )

            async def on_hour(interaction: discord.Interaction) -> None:
                self.hour = int(hour_sel.values[0])
                await self._refresh(interaction)

            hour_sel.callback = on_hour  # type: ignore[method-assign]
            self.add_item(hour_sel)

            min_sel = discord.ui.Select(
                placeholder="Minutes",
                options=[
                    discord.SelectOption(
                        label=f":{m:02d}",
                        value=str(m),
                        default=m == self.minute,
                    )
                    for m in (0, 15, 30, 45)
                ],
                row=3,
            )

            async def on_min(interaction: discord.Interaction) -> None:
                self.minute = int(min_sel.values[0])
                await self._refresh(interaction)

            min_sel.callback = on_min  # type: ignore[method-assign]
            self.add_item(min_sel)
            btn_row = 4

        confirm = discord.ui.Button(
            label="Confirm schedule",
            style=discord.ButtonStyle.success,
            row=btn_row,
        )
        toggle = discord.ui.Button(
            label="Day only" if self.exact else "Exact time…",
            style=discord.ButtonStyle.primary,
            row=btn_row,
        )
        back = discord.ui.Button(
            label="← Back",
            style=discord.ButtonStyle.secondary,
            row=btn_row,
        )

        async def on_confirm(interaction: discord.Interaction) -> None:
            await _commit_scheduled_start(
                interaction,
                view=self,
                ch=self.ch,
                focus=self.focus,
                guild_id=self.guild_id,
                offset=self.offset,
                day_rel=self.day_rel,
                hhmm=self._hhmm(),
                dry_run=self.dry_run,
                day_only=not self.exact,
            )

        async def on_toggle(interaction: discord.Interaction) -> None:
            self.exact = not self.exact
            self._build_items()
            await interaction.response.edit_message(
                embed=self._schedule_embed(), view=self
            )

        async def on_back(interaction: discord.Interaction) -> None:
            view = WhenPickView(
                self.owner_id,
                ch=self.ch,
                focus=self.focus,
                year=self.year,
                guild_id=self.guild_id,
                tier=self.tier,
                dry_run=self.dry_run,
            )
            msg = _release_view(self) or interaction.message
            await interaction.response.edit_message(
                embed=view._when_embed(), view=view
            )
            _bind_view_message(view, msg)

        confirm.callback = on_confirm  # type: ignore[method-assign]
        toggle.callback = on_toggle  # type: ignore[method-assign]
        back.callback = on_back  # type: ignore[method-assign]
        self.add_item(confirm)
        self.add_item(toggle)
        self.add_item(back)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        # Rebuild selects so Discord keeps the chosen defaults (no snap-back)
        self._build_items()
        await interaction.response.edit_message(
            embed=self._schedule_embed(), view=self
        )


# Back-compat aliases (testsched / old call sites)
ScheduleDayView = ScheduleView  # type: ignore[misc,assignment]


class WhenPickView(_OwnerView):
    def __init__(
        self,
        owner_id: int,
        *,
        ch: dict,
        focus: str | None,
        year: int,
        guild_id: str | None,
        tier: str,
        dry_run: bool = False,
    ) -> None:
        super().__init__(owner_id)
        self.ch = ch
        self.focus = focus
        self.year = year
        self.guild_id = guild_id
        self.tier = tier
        self.dry_run = dry_run
        self.needs_tadoku_link = (
            not dry_run
            and _challenge_needs_tadoku(ch)
            and not _user_tadoku_linked(owner_id)
        )
        if self.needs_tadoku_link:
            link_btn = discord.ui.Button(
                label="Link Tadoku…",
                style=discord.ButtonStyle.primary,
                row=2,
            )

            async def on_link(interaction: discord.Interaction) -> None:
                await interaction.response.send_modal(
                    LinkModal(
                        resume_message=interaction.message,
                        resume_view=self,
                        year=self.year,
                        guild_id=self.guild_id,
                        owner_id=self.owner_id,
                        resume_ch=self.ch,
                        resume_focus=self.focus,
                        resume_tier=self.tier,
                        dry_run=self.dry_run,
                    )
                )

            link_btn.callback = on_link  # type: ignore[method-assign]
            self.add_item(link_btn)

    def _when_embed(self) -> discord.Embed:
        banner = (
            "**TEST MODE — nothing will be saved.**\n\n"
            if self.dry_run
            else ""
        )
        need = ""
        if getattr(self, "needs_tadoku_link", False):
            need = (
                "**Tadoku required** for this challenge — "
                "tap **Link Tadoku…** below before starting.\n\n"
            )
        off = _default_offset_for(self.owner_id)
        today = _day_label(off, 0)
        return _start_embed(
            title=("TEST · " if self.dry_run else "") + "When do you want to start?",
            description=(
                f"{banner}{need}"
                f"**{self.ch['tier']} #{self.ch['number']}: {self.ch['title']}**\n\n"
                f"Timezone: `{off}` · {today}\n"
                "**Start now** or **Schedule** a day "
                "(optional exact time inside)."
            ),
            ch=self.ch,
        )

    @discord.ui.button(label="Start now", style=discord.ButtonStyle.success, row=0)
    async def start_now(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if (
            not self.dry_run
            and _challenge_needs_tadoku(self.ch)
            and not _user_tadoku_linked(self.owner_id)
        ):
            await interaction.response.send_message(
                "This challenge needs Tadoku linked first. "
                "Tap **Link Tadoku…** on this panel (or `/challenge link`), "
                "then **Start now**.",
                ephemeral=True,
            )
            return
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.dry_run:
            await interaction.response.edit_message(
                embed=_start_embed(
                    title="TEST start — nothing saved",
                    description=(
                        f"**{self.ch['tier']} #{self.ch['number']}: {self.ch['title']}**\n"
                        "Would **start now**. No attempt was created.\n"
                        "Run `/challenge start` for real."
                    ),
                    ch=self.ch,
                ),
                view=None,
            )
            return
        await interaction.response.edit_message(
            embed=_start_embed(
                title="Starting…",
                description=f"**{self.ch['tier']} #{self.ch['number']}: {self.ch['title']}**",
                ch=self.ch,
            ),
            view=self,
        )
        await _finalize_start(
            interaction,
            challenge_id=self.ch["id"],
            focus=self.focus,
            guild_id=self.guild_id,
            starts_at=None,
            restore_view=self,
        )

    @discord.ui.button(label="Schedule…", style=discord.ButtonStyle.primary, row=0)
    async def schedule(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        view = ScheduleView(
            self.owner_id,
            ch=self.ch,
            focus=self.focus,
            guild_id=self.guild_id,
            year=self.year,
            tier=self.tier,
            offset=_default_offset_for(self.owner_id),
            exact=False,
            dry_run=self.dry_run,
        )
        msg = _release_view(self) or interaction.message
        await interaction.response.edit_message(
            embed=view._schedule_embed(),
            view=view,
        )
        _bind_view_message(view, msg)

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.dry_run:
            self.stop()
            await interaction.response.edit_message(
                embed=_start_embed(
                    title="Test closed",
                    description="Schedule UI test ended — nothing was saved.",
                ),
                view=None,
            )
            return
        items = list_challenges(year=self.year, tier=self.tier)
        done_ids = att_svc.completed_challenge_ids(str(self.owner_id))
        pick = ChallengePickView(
            self.owner_id,
            tier=self.tier,
            year=self.year,
            guild_id=self.guild_id,
            challenges=items,
            done_ids=done_ids,
        )
        lines = [
            f"**#{c['number']} {c['title']}**"
            + (
                f" · needs *{c['param_label']}*"
                if needs_focus_param(c)
                else ""
            )
            + _challenge_done_tag(str(c["id"]), done_ids)
            for c in items
        ]
        msg = _release_view(self) or interaction.message
        await interaction.response.edit_message(
            embed=_start_embed(
                title=f"{self.tier} challenges",
                description=(
                    f"{_cleared_note()}\nSelect one:\n" + "\n".join(lines)
                ),
            ),
            view=pick,
        )
        _bind_view_message(pick, msg)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        msg = (
            "Schedule UI test ended — nothing was saved."
            if self.dry_run
            else "No challenge started."
        )
        await interaction.response.edit_message(
            embed=_start_embed(
                title="Test closed" if self.dry_run else "Cancelled",
                description=msg,
            ),
            view=None,
        )


class ChallengeSelect(discord.ui.Select):
    def __init__(
        self, challenges: list[dict], *, done_ids: set[str] | None = None
    ) -> None:
        self.done_ids = done_ids or set()
        lock = not att_svc.reattempts_allowed()
        options = []
        for c in challenges[:25]:
            cid = str(c["id"])
            done = cid in self.done_ids
            need = c.get("param_label") if needs_focus_param(c) else None
            if done and lock:
                desc = "Cleared — can't redo"
            elif done:
                desc = "Already cleared"
            else:
                desc = (c.get("description") or "")[:100]
                if need:
                    desc = f"Needs {need} · {desc}"[:100]
            options.append(
                discord.SelectOption(
                    label=f"#{c['number']} · {c['title']}"[:100],
                    value=cid,
                    description=desc or None,
                    emoji="✅" if done else None,
                )
            )
        super().__init__(
            placeholder="Choose a challenge…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: ChallengePickView = self.view  # type: ignore[assignment]
        ch = get_challenge(self.values[0])
        if not ch:
            await interaction.response.send_message(
                "Challenge not found — try again.", ephemeral=True
            )
            return
        if (
            str(ch["id"]) in self.done_ids
            and not att_svc.reattempts_allowed()
        ):
            await interaction.response.send_message(
                "You already cleared this challenge — each one is one-time only.",
                ephemeral=True,
            )
            return
        if needs_focus_param(ch):
            await interaction.response.send_modal(
                FocusModal(
                    ch,
                    owner_id=view.owner_id,
                    year=view.year,
                    guild_id=view.guild_id,
                    tier=view.tier,
                )
            )
            return
        when = WhenPickView(
            view.owner_id,
            ch=ch,
            focus=None,
            year=view.year,
            guild_id=view.guild_id,
            tier=view.tier,
        )
        msg = _release_view(view) or interaction.message
        await interaction.response.edit_message(
            embed=when._when_embed(),
            view=when,
        )
        _bind_view_message(when, msg)


class ChallengePickView(_OwnerView):
    def __init__(
        self,
        owner_id: int,
        *,
        tier: str,
        year: int,
        guild_id: str | None,
        challenges: list[dict],
        done_ids: set[str] | None = None,
    ) -> None:
        super().__init__(owner_id)
        self.tier = tier
        self.year = year
        self.guild_id = guild_id
        self.done_ids = done_ids or set()
        self.add_item(ChallengeSelect(challenges, done_ids=self.done_ids))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        nxt = TierPickView(self.owner_id, year=self.year, guild_id=self.guild_id)
        msg = _release_view(self) or interaction.message
        await interaction.response.edit_message(
            embed=_start_embed(
                description="Pick a **difficulty**, then a challenge from the list.",
            ),
            view=nxt,
        )
        _bind_view_message(nxt, msg)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=_start_embed(title="Cancelled", description="No challenge started."),
            view=None,
        )


class TierButton(discord.ui.Button):
    def __init__(self, tier: str, label: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, style=style)
        self.tier = tier

    async def callback(self, interaction: discord.Interaction) -> None:
        view: TierPickView = self.view  # type: ignore[assignment]
        items = list_challenges(year=view.year, tier=self.tier)
        if not items:
            await interaction.response.send_message(
                f"No challenges in **{self.label}** for {view.year}.",
                ephemeral=True,
            )
            return
        done_ids = att_svc.completed_challenge_ids(str(view.owner_id))
        if view.mode == "list":
            b = _bot_helpers()
            lines = [_cleared_note(), ""]
            for c in items:
                param = (
                    f" `<{c['param_label']}>`" if needs_focus_param(c) else ""
                )
                tag = _challenge_done_tag(str(c["id"]), done_ids)
                lines.append(
                    f"**#{c['number']} {c['title']}**{param}{tag} — "
                    f"{c.get('description') or ''} "
                    f"_(confirm: {b.verify_label(c)})_"
                )
            embed = discord.Embed(
                title=f"{self.label} · {view.year}",
                description="\n".join(lines)[:4000],
                color=0x5865F2,
            )
            back = ListTierView(view.owner_id, year=view.year)
            await interaction.response.edit_message(embed=embed, view=back)
            back.message = interaction.message
            return
        lines = []
        for c in items:
            need = (
                f" · needs *{c['param_label']}*"
                if needs_focus_param(c)
                else ""
            )
            tag = _challenge_done_tag(str(c["id"]), done_ids)
            lines.append(f"**#{c['number']} {c['title']}**{need}{tag}")
        pick = ChallengePickView(
            view.owner_id,
            tier=self.tier,
            year=view.year,
            guild_id=view.guild_id,
            challenges=items,
            done_ids=done_ids,
        )
        msg = _release_view(view) or interaction.message
        await interaction.response.edit_message(
            embed=_start_embed(
                title=f"{self.label} challenges",
                description=(
                    f"{_cleared_note()}\nSelect one:\n" + "\n".join(lines)
                ),
            ),
            view=pick,
        )
        _bind_view_message(pick, msg)


class TierPickView(_OwnerView):
    def __init__(
        self,
        owner_id: int,
        *,
        year: int,
        guild_id: str | None,
        mode: str = "start",
    ) -> None:
        super().__init__(owner_id)
        self.year = year
        self.guild_id = guild_id
        self.mode = mode  # start | list
        for tier, label, style in TIER_META:
            self.add_item(TierButton(tier, label, style))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=_start_embed(title="Cancelled", description="Closed."),
            view=None,
        )


class ListTierView(TierPickView):
    def __init__(self, owner_id: int, *, year: int) -> None:
        super().__init__(owner_id, year=year, guild_id=None, mode="list")


# ----- status / confirm / abandon -----


class StatusActionsView(_OwnerView):
    def __init__(
        self,
        owner_id: int,
        *,
        has_active: bool,
        attempt_id: int | None = None,
        attempt: dict | None = None,
    ) -> None:
        super().__init__(owner_id)
        self.attempt_id = attempt_id
        self.attempt = attempt
        if has_active:
            confirm = discord.ui.Button(
                label="Confirm",
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def _confirm(interaction: discord.Interaction) -> None:
                att = self.attempt
                if att is None and self.attempt_id is not None:
                    picked = att_svc.resolve_user_attempt(
                        str(interaction.user.id),
                        attempt_id=self.attempt_id,
                        statuses=("active",),
                    )
                    att = picked.get("attempt") if picked.get("ok") else None
                if not att:
                    await interaction.response.send_message(
                        "No active challenge.", ephemeral=True
                    )
                    return
                await _begin_confirm(interaction, att)

            confirm.callback = _confirm  # type: ignore[method-assign]
            self.add_item(confirm)

        abandon = discord.ui.Button(
            label="Abandon / cancel",
            style=discord.ButtonStyle.danger,
            row=1,
        )

        async def _abandon(interaction: discord.Interaction) -> None:
            view = AbandonConfirmView(self.owner_id, attempt_id=self.attempt_id)
            await interaction.response.send_message(
                "Really abandon your current challenge?",
                view=view,
                ephemeral=True,
            )

        abandon.callback = _abandon  # type: ignore[method-assign]
        self.add_item(abandon)


class FailOverridePromptView(_OwnerView):
    """Shown after a failed confirm — opens hard honor override modal."""

    def __init__(
        self,
        owner_id: int,
        *,
        attempt_id: int,
        content_hint: str | None = None,
    ) -> None:
        super().__init__(owner_id, timeout=300)
        self.attempt_id = int(attempt_id)
        self.content_hint = content_hint
        btn = discord.ui.Button(
            label="Fail override (honor)…",
            style=discord.ButtonStyle.danger,
            row=0,
        )

        async def _go(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(
                FailOverrideModal(
                    attempt_id=self.attempt_id,
                    content_hint=self.content_hint,
                )
            )

        btn.callback = _go  # type: ignore[method-assign]
        self.add_item(btn)


class FailOverrideModal(discord.ui.Modal, title="Fail override (honor)"):
    """Hard user backdoor: complete without Tadoku."""

    def __init__(
        self,
        *,
        attempt_id: int,
        content_hint: str | None = None,
    ) -> None:
        super().__init__()
        self.attempt_id = int(attempt_id)
        _hint = (content_hint or "").replace(", ", "\n")
        self.content_in = discord.ui.TextInput(
            label="Works completed (one per line)",
            placeholder="Required — one title/URL per line",
            default=_hint[:1800],
            required=True,
            max_length=1800,
            style=discord.TextStyle.paragraph,
        )
        self.note_in = discord.ui.TextInput(
            label="Why override? (optional)",
            placeholder="e.g. Tadoku title misspelled / API down",
            required=False,
            max_length=200,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.content_in)
        self.add_item(self.note_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _run_confirm(
            interaction,
            content=str(self.content_in.value).strip() or None,
            note=str(self.note_in.value).strip() or None,
            filter_mode="honor",
            attempt_id=self.attempt_id,
        )


class HonorConfirmModal(discord.ui.Modal, title="Confirm challenge"):
    """Single field when honor complete_work needs a named work."""

    def __init__(
        self,
        *,
        attempt_id: int,
        content_hint: str | None = None,
    ) -> None:
        super().__init__()
        self.attempt_id = int(attempt_id)
        _hint = (content_hint or "").replace(", ", "\n")
        self.content_in = discord.ui.TextInput(
            label="What you completed",
            placeholder="Series, book, URL — one per line",
            default=_hint[:1800],
            required=True,
            max_length=1800,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.content_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _run_confirm(
            interaction,
            content=str(self.content_in.value).strip() or None,
            attempt_id=self.attempt_id,
        )


def _confirm_needs_content(att: dict) -> bool:
    """True when honor complete_work has no focus/content yet."""
    if (att.get("verify") or "tadoku").lower() != "honor":
        return False
    if (att.get("goal_kind") or "") != "complete_work":
        return False
    if (att.get("param") or "").strip():
        return False
    if (att.get("content") or "").strip():
        return False
    return True


async def _begin_confirm(
    interaction: discord.Interaction, att: dict
) -> None:
    """Tadoku: verify immediately. Honor needing a name: one-field modal."""
    if _confirm_needs_content(att):
        await interaction.response.send_modal(
            HonorConfirmModal(
                attempt_id=int(att["id"]),
                content_hint=att_svc.display_content(att) or None,
            )
        )
        return
    await _run_confirm(interaction, attempt_id=int(att["id"]))


class AbandonConfirmView(_OwnerView):
    def __init__(self, owner_id: int, *, attempt_id: int | None = None) -> None:
        super().__init__(owner_id)
        self.attempt_id = attempt_id

    @discord.ui.button(label="Yes, abandon", style=discord.ButtonStyle.danger)
    async def yes(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await _run_abandon(interaction, attempt_id=self.attempt_id)
        try:
            await interaction.edit_original_response(content="Done.", view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary)
    async def no(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(content="Kept.", view=None)


class LinkModal(discord.ui.Modal, title="Link Tadoku"):
    user_id = discord.ui.TextInput(
        label="Tadoku user id or profile URL",
        placeholder="UUID or https://tadoku.app/...",
        required=True,
        max_length=200,
    )
    display_name = discord.ui.TextInput(
        label="Display name (optional)",
        required=False,
        max_length=80,
    )

    def __init__(
        self,
        *,
        resume_message: discord.Message | None = None,
        resume_view: discord.ui.View | None = None,
        year: int | None = None,
        guild_id: str | None = None,
        owner_id: int | None = None,
        resume_ch: dict | None = None,
        resume_focus: str | None = None,
        resume_tier: str | None = None,
        dry_run: bool = False,
    ) -> None:
        super().__init__()
        self.resume_message = resume_message
        self.resume_view = resume_view
        self.year = year
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.resume_ch = resume_ch
        self.resume_focus = resume_focus
        self.resume_tier = resume_tier
        self.dry_run = dry_run

    async def _push_resume_panel(
        self,
        interaction: discord.Interaction,
        *,
        tip: str,
        view: discord.ui.View,
        embed: discord.Embed,
    ) -> None:
        # Detach prior wizard step so its timeout cannot disable this panel.
        owned = _release_view(self.resume_view)
        msg = self.resume_message or owned
        await interaction.response.send_message(tip, ephemeral=True)
        if msg is not None:
            try:
                await msg.edit(embed=embed, view=view)
                _bind_view_message(view, msg)
                return
            except discord.HTTPException:
                logger.warning("could not edit start panel after Tadoku link")
        try:
            sent = await interaction.followup.send(
                embed=embed, view=view, ephemeral=True
            )
            _bind_view_message(view, sent)
        except discord.HTTPException:
            logger.warning("could not send fallback start panel after Tadoku link")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.user_id.value).strip()
        m = re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            raw,
            re.I,
        )
        if not m:
            await interaction.response.send_message(
                "Could not find a UUID. Paste your Tadoku **user id** "
                "(looks like `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`) "
                "or your full profile URL from tadoku.app.",
                ephemeral=True,
            )
            return
        name = str(self.display_name.value).strip() or None
        att_svc.link_tadoku(str(interaction.user.id), m.group(0), name)
        linked = m.group(0)
        owner = self.owner_id if self.owner_id is not None else interaction.user.id
        year = self.year if self.year is not None else _catalog_year()

        # Resume mid-start "when" step (Skip -> pick Tadoku challenge -> link here).
        if self.resume_ch is not None:
            view = _when_pick_for(
                owner,
                ch=self.resume_ch,
                focus=self.resume_focus,
                guild_id=self.guild_id,
                year=year,
                tier=self.resume_tier,
                dry_run=self.dry_run,
            )
            await self._push_resume_panel(
                interaction,
                tip=f"Linked Tadoku `{linked}`. Start or schedule below.",
                view=view,
                embed=view._when_embed(),
            )
            return

        # Resume start wizard at difficulty pick.
        if (
            self.resume_message is not None
            or self.resume_view is not None
            or self.year is not None
        ):
            view = TierPickView(owner, year=year, guild_id=self.guild_id)
            await self._push_resume_panel(
                interaction,
                tip=f"Linked Tadoku `{linked}`. Pick a difficulty below.",
                view=view,
                embed=_start_tier_embed(
                    year=year, note=f"Tadoku linked · `{linked}`"
                ),
            )
            return

        await interaction.response.send_message(
            f"Linked Tadoku user `{linked}`. Use `/challenge start` when ready.",
            ephemeral=True,
        )
# ----- admin panel -----


def _fmt_test_result(result: dict) -> str:
    lines = []
    if result.get("error"):
        lines.append(f"**Error:** {result['error']}")
    if result.get("dry_run"):
        lines.append(
            f"**Dry-run** — would_complete={result.get('would_complete')} "
            f"method=`{result.get('method')}`"
        )
    elif result.get("ok"):
        lines.append(f"**Applied** method=`{result.get('method')}`")
    if result.get("progress"):
        lines.append(f"Progress: {result['progress']}")
    if result.get("log_count") is not None:
        lines.append(
            f"Logs: **{result['log_count']}** (source=`{result.get('log_source')}`)"
        )
    if result.get("tadoku_user_id"):
        lines.append(f"Tadoku: `{result['tadoku_user_id']}`")
    att = result.get("attempt") or {}
    if att.get("title"):
        lines.append(
            f"Attempt: **{att.get('tier')} #{att.get('number')} {att.get('title')}** "
            f"(id={att.get('id')})"
        )
    v = result.get("verify") or {}
    if v:
        lines.append(
            f"Verify: ok={v.get('ok')} {v.get('progress')} / {v.get('target')} "
            f"{v.get('unit_label')}"
        )
    return "\n".join(lines) or str(result)[:1500]


class AdminPanelView(_OwnerView):
    def __init__(
        self, owner_id: int, target: discord.abc.User, guild: discord.Guild | None
    ) -> None:
        super().__init__(owner_id, timeout=300)
        self.target = target
        self.guild = guild

    async def _run_mode(
        self,
        interaction: discord.Interaction,
        *,
        mode: str,
        apply: bool,
        do_announce: bool = False,
        do_rewards: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await att_svc.admin_test_verify(
            str(self.target.id),
            mode=mode,
            apply=apply,
            note=f"admin {mode} by {interaction.user.id}",
        )
        extra = ""
        if apply and result.get("ok") and not result.get("dry_run"):
            att = result.get("attempt") or {}
            member = None
            if self.guild:
                member = self.guild.get_member(self.target.id)
            if do_rewards and member is not None:
                got = await apply_rewards(member, tier=att.get("tier") or "easy")
                extra += f"\nRewards: {', '.join(got) or 'none'}"
            if do_announce and self.guild:
                await announce_completion(
                    interaction.client,
                    guild=self.guild,
                    user=self.target,
                    title=att.get("title") or "?",
                    tier=att.get("tier") or "?",
                    number=int(att.get("number") or 0),
                    param=att.get("param"),
                    content=result.get("content") or att_svc.display_content(att),
                    method=str(result.get("method") or mode),
                )
                extra += "\nAnnounced."
        await interaction.followup.send(
            f"**{mode}** apply={apply} → {self.target.mention}\n"
            f"{_fmt_test_result(result)}{extra}",
            ephemeral=True,
        )

    @discord.ui.button(label="Probe Tadoku link", style=discord.ButtonStyle.secondary, row=0)
    async def probe(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await att_svc.probe_tadoku_link(str(self.target.id), hours_back=48.0)
        if not result.get("ok"):
            await interaction.followup.send(result.get("error") or "Failed", ephemeral=True)
            return
        lines = [
            f"**Tadoku probe** {self.target.mention}",
            f"id: `{result['tadoku_user_id']}` · logs 48h: **{result['log_count']}**",
        ]
        for s in (result.get("samples") or [])[:5]:
            lines.append(
                f"- `{s.get('created_at')}` {s.get('amount')} {s.get('unit')} · {s.get('description')}"
            )
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    @discord.ui.button(label="Preview real (dry)", style=discord.ButtonStyle.primary, row=0)
    async def preview(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._run_mode(interaction, mode="preview_real", apply=False)

    @discord.ui.button(label="Fake pass (dry)", style=discord.ButtonStyle.primary, row=1)
    async def fake_dry(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._run_mode(interaction, mode="fake_pass", apply=False)

    @discord.ui.button(label="Fake pass APPLY", style=discord.ButtonStyle.danger, row=1)
    async def fake_apply(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._run_mode(
            interaction, mode="fake_pass", apply=True, do_announce=True, do_rewards=True
        )

    @discord.ui.button(label="Force complete", style=discord.ButtonStyle.danger, row=2)
    async def force(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._run_mode(
            interaction, mode="force", apply=True, do_announce=True, do_rewards=True
        )

    @discord.ui.button(label="Void attempt id…", style=discord.ButtonStyle.secondary, row=2)
    async def void_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(VoidModal())


class VoidModal(discord.ui.Modal, title="Void attempt"):
    attempt_id = discord.ui.TextInput(label="Attempt id", placeholder="123", required=True)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        b = _bot_helpers()
        if not b.is_bot_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        try:
            aid = int(str(self.attempt_id.value).strip())
        except ValueError:
            await interaction.response.send_message("Need a number.", ephemeral=True)
            return
        r = att_svc.admin_update_attempt(aid, status="void", verify_method="mod")
        await interaction.response.send_message(
            "Voided." if r.get("ok") else r.get("error"), ephemeral=True
        )


# ----- admin catalog add / edit / remove -----

_GOAL_KINDS = (
    "volumes",
    "episodes",
    "minutes",
    "characters",
    "immersion_hours",
    "complete_work",
)
_MEDIA_FILTERS = (
    "any",
    "manga",
    "shows",
    "youtube",
    "vn",
    "reading",
    "book",
)


def _admin_only(interaction: discord.Interaction) -> bool:
    return bool(_bot_helpers().is_bot_admin(interaction))


def _next_challenge_number(year: int, tier: str) -> int:
    items = list_challenges(year=year, tier=tier)
    return max((int(c["number"]) for c in items), default=0) + 1


def _challenge_payload(ch: dict, **overrides: object) -> dict:
    """Build admin_upsert_challenge dict from a row + field overrides."""
    base = {
        "id": ch.get("id"),
        "year": ch.get("year"),
        "tier": ch.get("tier"),
        "number": ch.get("number"),
        "title": ch.get("title"),
        "description": ch.get("description") or "",
        "time_limit_hours": ch.get("time_limit_hours") or 24,
        "param_label": ch.get("param_label"),
        "verify": ch.get("verify") or "tadoku",
        "goal_kind": ch.get("goal_kind"),
        "goal_amount": ch.get("goal_amount"),
        "media_filter": ch.get("media_filter") or "any",
        "allows_concurrent": ch.get("allows_concurrent") or 0,
        "fixed_param": ch.get("fixed_param"),
    }
    base.update(overrides)
    return base


def _catalog_embed(
    *,
    title: str,
    description: str,
    ch: dict | None = None,
) -> discord.Embed:
    emb = discord.Embed(title=title, description=description[:4000], color=0xED4245)
    if ch:
        emb.add_field(
            name="Definition",
            value=(
                f"`{ch.get('id')}` · {ch.get('tier')} #{ch.get('number')} · "
                f"{ch.get('year')}\n"
                f"**{ch.get('title')}**\n"
                f"goal `{ch.get('goal_kind')}` {ch.get('goal_amount')} · "
                f"media `{ch.get('media_filter') or 'any'}` · "
                f"verify **{ch.get('verify') or 'tadoku'}** · "
                f"{ch.get('time_limit_hours') or 24}h"
                + (
                    f" · param `{ch.get('param_label')}`"
                    if ch.get("param_label")
                    else ""
                )
                + (
                    " · concurrent"
                    if int(ch.get("allows_concurrent") or 0)
                    else ""
                )
            )[:1024],
            inline=False,
        )
        if ch.get("description"):
            emb.add_field(
                name="Details", value=str(ch["description"])[:300], inline=False
            )
    emb.set_footer(text="Admin only · times out in 3 min")
    return emb


class _AdminCatalogSelect(discord.ui.Select):
    """Pick a challenge; parent view must implement on_challenge_picked."""

    def __init__(self, challenges: list[dict]) -> None:
        options = [
            discord.SelectOption(
                label=f"#{c['number']} · {c['title']}"[:100],
                value=str(c["id"]),
                description=(c.get("description") or c.get("id") or "")[:100] or None,
            )
            for c in challenges[:25]
        ]
        super().__init__(
            placeholder="Choose a challenge…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: _AdminPickView = self.view  # type: ignore[assignment]
        if not _admin_only(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        ch = get_challenge(self.values[0])
        if not ch:
            await interaction.response.send_message(
                "Challenge not found.", ephemeral=True
            )
            return
        await view.on_challenge_picked(interaction, ch)


class _AdminPickView(_OwnerView):
    def __init__(
        self,
        owner_id: int,
        *,
        year: int,
        tier: str,
        challenges: list[dict],
        mode: str,
    ) -> None:
        super().__init__(owner_id)
        self.year = year
        self.tier = tier
        self.mode = mode  # edit | remove
        self.add_item(_AdminCatalogSelect(challenges))

    async def on_challenge_picked(
        self, interaction: discord.Interaction, ch: dict
    ) -> None:
        if self.mode == "remove":
            conf = AdminRemoveConfirmView(self.owner_id, ch=ch, year=self.year)
            await interaction.response.edit_message(
                embed=_catalog_embed(
                    title="Remove challenge?",
                    description=(
                        "Deletes the **catalog row** only. "
                        "Past attempts keep their challenge id.\n"
                        "This cannot be undone from Discord."
                    ),
                    ch=ch,
                ),
                view=conf,
            )
            conf.message = interaction.message
            return
        edit = AdminEditView(self.owner_id, ch=ch, year=self.year)
        await interaction.response.edit_message(
            embed=edit._embed(),
            view=edit,
        )
        edit.message = interaction.message

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        view = AdminCatalogTierView(
            self.owner_id, year=self.year, mode=self.mode
        )
        await interaction.response.edit_message(
            embed=_catalog_embed(
                title=f"Admin · {self.mode.title()} challenge",
                description="Pick a **difficulty**.",
            ),
            view=view,
        )
        view.message = interaction.message

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=_catalog_embed(title="Cancelled", description="No changes."),
            view=None,
        )


class AdminCatalogTierButton(discord.ui.Button):
    def __init__(self, tier: str, label: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, style=style)
        self.tier = tier

    async def callback(self, interaction: discord.Interaction) -> None:
        view: AdminCatalogTierView = self.view  # type: ignore[assignment]
        if not _admin_only(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        if view.mode == "add":
            setup = AdminAddSetupView(
                view.owner_id, year=view.year, tier=self.tier
            )
            await interaction.response.edit_message(
                embed=setup._embed(),
                view=setup,
            )
            setup.message = interaction.message
            return
        items = list_challenges(year=view.year, tier=self.tier)
        if not items:
            await interaction.response.send_message(
                f"No **{self.label}** challenges for {view.year}.",
                ephemeral=True,
            )
            return
        lines = [f"**#{c['number']} {c['title']}** (`{c['id']}`)" for c in items]
        pick = _AdminPickView(
            view.owner_id,
            year=view.year,
            tier=self.tier,
            challenges=items,
            mode=view.mode,
        )
        await interaction.response.edit_message(
            embed=_catalog_embed(
                title=f"{self.label} · {view.mode}",
                description="Select one:\n" + "\n".join(lines),
            ),
            view=pick,
        )
        pick.message = interaction.message


class AdminCatalogTierView(_OwnerView):
    def __init__(self, owner_id: int, *, year: int, mode: str) -> None:
        super().__init__(owner_id)
        self.year = year
        self.mode = mode  # add | edit | remove
        for tier, label, style in TIER_META:
            self.add_item(AdminCatalogTierButton(tier, label, style))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=_catalog_embed(title="Cancelled", description="No changes."),
            view=None,
        )


class AdminRemoveConfirmView(_OwnerView):
    def __init__(self, owner_id: int, *, ch: dict, year: int) -> None:
        super().__init__(owner_id)
        self.ch = ch
        self.year = year

    @discord.ui.button(label="Delete permanently", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not _admin_only(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        cid = str(self.ch["id"])
        counts = att_svc.attempt_status_counts_for_challenge(cid)
        att_svc.admin_delete_challenge(cid)
        self.stop()
        n = int(counts.get("total") or 0)
        await interaction.response.edit_message(
            embed=_catalog_embed(
                title="Challenge removed",
                description=(
                    f"Deleted `{cid}` from the catalog."
                    + (f" ({n} historical attempt(s) kept.)" if n else "")
                ),
            ),
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=_catalog_embed(title="Cancelled", description="Nothing deleted."),
            view=None,
        )


class AdminEditModal(discord.ui.Modal, title="Edit challenge"):
    def __init__(self, owner_id: int, ch: dict, year: int) -> None:
        super().__init__()
        self.owner_id = owner_id
        self.ch = dict(ch)
        self.year = year
        self.title_in = discord.ui.TextInput(
            label="Title",
            default=str(ch.get("title") or "")[:100],
            required=True,
            max_length=100,
        )
        self.goal_amount_in = discord.ui.TextInput(
            label="Goal amount",
            default=str(ch.get("goal_amount") or "1"),
            required=True,
            max_length=20,
        )
        self.hours_in = discord.ui.TextInput(
            label="Time limit (hours)",
            default=str(ch.get("time_limit_hours") or "24"),
            required=True,
            max_length=10,
        )
        self.param_in = discord.ui.TextInput(
            label="Param label (blank = none)",
            default=str(ch.get("param_label") or ""),
            required=False,
            max_length=40,
            placeholder="series / game / …",
        )
        self.desc_in = discord.ui.TextInput(
            label="Description",
            default=str(ch.get("description") or "")[:500],
            required=False,
            max_length=500,
            style=discord.TextStyle.paragraph,
        )
        for item in (
            self.title_in,
            self.goal_amount_in,
            self.hours_in,
            self.param_in,
            self.desc_in,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _admin_only(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        try:
            amount = float(str(self.goal_amount_in.value).strip())
            hours = float(str(self.hours_in.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "Goal amount and hours must be numbers.", ephemeral=True
            )
            return
        param = str(self.param_in.value or "").strip() or None
        r = att_svc.admin_upsert_challenge(
            _challenge_payload(
                self.ch,
                title=str(self.title_in.value).strip(),
                goal_amount=amount,
                time_limit_hours=hours,
                param_label=param,
                description=str(self.desc_in.value or "").strip(),
            )
        )
        if not r.get("ok"):
            await interaction.response.send_message(
                r.get("error") or "Save failed.", ephemeral=True
            )
            return
        ch = get_challenge(str(self.ch["id"])) or self.ch
        edit = AdminEditView(self.owner_id, ch=ch, year=self.year)
        await interaction.response.send_message(
            embed=edit._embed(),
            view=edit,
            ephemeral=True,
        )
        edit.message = await interaction.original_response()


class _AdminEditGoalSelect(discord.ui.Select):
    def __init__(self, current: str) -> None:
        options = [
            discord.SelectOption(label=g, value=g, default=(g == current))
            for g in _GOAL_KINDS
        ]
        super().__init__(
            placeholder="Goal kind…",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: AdminEditView = self.view  # type: ignore[assignment]
        await view._apply(interaction, goal_kind=self.values[0])


class _AdminEditMediaSelect(discord.ui.Select):
    def __init__(self, current: str) -> None:
        cur = current if current in _MEDIA_FILTERS else "any"
        options = [
            discord.SelectOption(label=m, value=m, default=(m == cur))
            for m in _MEDIA_FILTERS
        ]
        super().__init__(
            placeholder="Media filter…",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: AdminEditView = self.view  # type: ignore[assignment]
        await view._apply(interaction, media_filter=self.values[0])


class AdminEditView(_OwnerView):
    def __init__(self, owner_id: int, *, ch: dict, year: int) -> None:
        super().__init__(owner_id, timeout=300)
        self.ch = dict(ch)
        self.year = year
        self.add_item(_AdminEditGoalSelect(str(ch.get("goal_kind") or "volumes")))
        self.add_item(
            _AdminEditMediaSelect(str(ch.get("media_filter") or "any"))
        )

    def _embed(self) -> discord.Embed:
        return _catalog_embed(
            title="Edit challenge",
            description=(
                "Change goal/media below, or **Edit fields…** for title/amount/hours.\n"
                "**Verify** / **Concurrent** toggle immediately."
            ),
            ch=self.ch,
        )

    async def _apply(self, interaction: discord.Interaction, **overrides: object) -> None:
        if not _admin_only(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        r = att_svc.admin_upsert_challenge(_challenge_payload(self.ch, **overrides))
        if not r.get("ok"):
            await interaction.response.send_message(
                r.get("error") or "Save failed.", ephemeral=True
            )
            return
        fresh = get_challenge(str(self.ch["id"]))
        if fresh:
            self.ch = fresh
        # Rebuild selects so defaults match saved values
        new = AdminEditView(self.owner_id, ch=self.ch, year=self.year)
        await interaction.response.edit_message(embed=new._embed(), view=new)
        new.message = interaction.message

    @discord.ui.button(
        label="Edit fields…", style=discord.ButtonStyle.primary, row=2
    )
    async def fields(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not _admin_only(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        await interaction.response.send_modal(
            AdminEditModal(self.owner_id, self.ch, self.year)
        )

    @discord.ui.button(
        label="Toggle verify", style=discord.ButtonStyle.secondary, row=2
    )
    async def toggle_verify(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        cur = (self.ch.get("verify") or "tadoku").lower()
        nxt = "honor" if cur == "tadoku" else "tadoku"
        await self._apply(interaction, verify=nxt)

    @discord.ui.button(
        label="Toggle concurrent", style=discord.ButtonStyle.secondary, row=2
    )
    async def toggle_concurrent(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        cur = int(self.ch.get("allows_concurrent") or 0)
        await self._apply(interaction, allows_concurrent=0 if cur else 1)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, row=3)
    async def done(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=_catalog_embed(
                title="Saved",
                description=f"`{self.ch.get('id')}` is up to date.",
                ch=self.ch,
            ),
            view=None,
        )


class _AdminAddGoalSelect(discord.ui.Select):
    def __init__(self, current: str) -> None:
        options = [
            discord.SelectOption(label=g, value=g, default=(g == current))
            for g in _GOAL_KINDS
        ]
        super().__init__(
            placeholder="Goal kind…",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: AdminAddSetupView = self.view  # type: ignore[assignment]
        view.goal_kind = self.values[0]
        await interaction.response.edit_message(embed=view._embed(), view=view)


class _AdminAddMediaSelect(discord.ui.Select):
    def __init__(self, current: str) -> None:
        options = [
            discord.SelectOption(label=m, value=m, default=(m == current))
            for m in _MEDIA_FILTERS
        ]
        super().__init__(
            placeholder="Media filter…",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: AdminAddSetupView = self.view  # type: ignore[assignment]
        view.media_filter = self.values[0]
        await interaction.response.edit_message(embed=view._embed(), view=view)


class AdminAddModal(discord.ui.Modal, title="New challenge"):
    def __init__(self, setup: "AdminAddSetupView") -> None:
        super().__init__()
        self.setup = setup
        nxt = _next_challenge_number(setup.year, setup.tier)
        self.number_in = discord.ui.TextInput(
            label="Number (id becomes tier-N)",
            default=str(nxt),
            required=True,
            max_length=4,
        )
        self.title_in = discord.ui.TextInput(
            label="Title",
            required=True,
            max_length=100,
            placeholder="Manga Mini-Marathon",
        )
        self.goal_amount_in = discord.ui.TextInput(
            label="Goal amount",
            default="1",
            required=True,
            max_length=20,
        )
        self.hours_in = discord.ui.TextInput(
            label="Time limit (hours)",
            default="24",
            required=True,
            max_length=10,
        )
        self.param_in = discord.ui.TextInput(
            label="Param label (blank = none)",
            required=False,
            max_length=40,
            placeholder="series / game / …",
        )
        for item in (
            self.number_in,
            self.title_in,
            self.goal_amount_in,
            self.hours_in,
            self.param_in,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _admin_only(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        s = self.setup
        try:
            number = int(str(self.number_in.value).strip())
            amount = float(str(self.goal_amount_in.value).strip())
            hours = float(str(self.hours_in.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "Number, goal amount, and hours must be numbers.",
                ephemeral=True,
            )
            return
        if number < 1:
            await interaction.response.send_message(
                "Number must be ≥ 1.", ephemeral=True
            )
            return
        cid = f"{s.tier}-{number}"
        if get_challenge(cid):
            await interaction.response.send_message(
                f"`{cid}` already exists — pick another number or edit it.",
                ephemeral=True,
            )
            return
        existing = list_challenges(year=s.year, tier=s.tier)
        if any(int(c["number"]) == number for c in existing):
            await interaction.response.send_message(
                f"{s.tier} #{number} already used this year.",
                ephemeral=True,
            )
            return
        param = str(self.param_in.value or "").strip() or None
        r = att_svc.admin_upsert_challenge(
            {
                "id": cid,
                "year": s.year,
                "tier": s.tier,
                "number": number,
                "title": str(self.title_in.value).strip(),
                "description": "",
                "time_limit_hours": hours,
                "param_label": param,
                "verify": s.verify,
                "goal_kind": s.goal_kind,
                "goal_amount": amount,
                "media_filter": s.media_filter,
                "allows_concurrent": s.allows_concurrent,
            }
        )
        if not r.get("ok"):
            await interaction.response.send_message(
                r.get("error") or "Create failed.", ephemeral=True
            )
            return
        ch = get_challenge(cid)
        # Description optional: open edit fields next if they want — show edit view
        edit = AdminEditView(s.owner_id, ch=ch or {"id": cid}, year=s.year)
        await interaction.response.send_message(
            embed=_catalog_embed(
                title="Challenge created",
                description=(
                    f"Added `{cid}`. Adjust description / options below if needed, "
                    "then **Done**."
                ),
                ch=ch,
            ),
            view=edit,
            ephemeral=True,
        )
        edit.message = await interaction.original_response()


class AdminAddSetupView(_OwnerView):
    def __init__(self, owner_id: int, *, year: int, tier: str) -> None:
        super().__init__(owner_id, timeout=300)
        self.year = year
        self.tier = tier
        self.goal_kind = "volumes"
        self.media_filter = "any"
        self.verify = "tadoku"
        self.allows_concurrent = 0
        self.add_item(_AdminAddGoalSelect(self.goal_kind))
        self.add_item(_AdminAddMediaSelect(self.media_filter))

    def _embed(self) -> discord.Embed:
        nxt = _next_challenge_number(self.year, self.tier)
        return _catalog_embed(
            title=f"Add · {self.tier}",
            description=(
                f"Year **{self.year}** · next number **#{nxt}** "
                f"(id `{self.tier}-{nxt}`)\n"
                f"goal `{self.goal_kind}` · media `{self.media_filter}` · "
                f"verify **{self.verify}**"
                + (" · concurrent" if self.allows_concurrent else "")
                + "\nSet options, then **Continue…**"
            ),
        )

    @discord.ui.button(
        label="Toggle verify", style=discord.ButtonStyle.secondary, row=2
    )
    async def toggle_verify(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.verify = "honor" if self.verify == "tadoku" else "tadoku"
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(
        label="Toggle concurrent", style=discord.ButtonStyle.secondary, row=2
    )
    async def toggle_concurrent(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.allows_concurrent = 0 if self.allows_concurrent else 1
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(
        label="Continue…", style=discord.ButtonStyle.primary, row=3
    )
    async def cont(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not _admin_only(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        await interaction.response.send_modal(AdminAddModal(self))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        view = AdminCatalogTierView(self.owner_id, year=self.year, mode="add")
        await interaction.response.edit_message(
            embed=_catalog_embed(
                title="Admin · Add challenge",
                description="Pick a **difficulty** for the new challenge.",
            ),
            view=view,
        )
        view.message = interaction.message


# ----- slash commands -----


def _start_tier_embed(*, year: int, note: str = "") -> discord.Embed:
    head = f"{note}\n" if note else ""
    return _start_embed(
        title="Pick difficulty",
        description=(
            f"{head}**{year}** catalog\n"
            "1️⃣ Difficulty → 2️⃣ Challenge → 3️⃣ Focus if needed → "
            "4️⃣ **Now** or **Schedule**"
        ),
    )


async def _advance_start_after_timezone(
    interaction: discord.Interaction,
    *,
    owner_id: int,
    year: int,
    guild_id: str | None,
    offset: str,
    prev_view: discord.ui.View | None = None,
) -> None:
    """Timezone done → Tadoku link gate (if needed) → tier pick."""
    msg = _release_view(prev_view) or interaction.message
    linked = _user_tadoku_linked(owner_id)
    note = f"Timezone **`{offset}`** · **{_day_label(offset, 0)}**"
    if linked:
        view = TierPickView(owner_id, year=year, guild_id=guild_id)
        await interaction.response.edit_message(
            embed=_start_tier_embed(year=year, note=note),
            view=view,
        )
        _bind_view_message(view, msg)
        return
    view = StartTadokuGateView(
        owner_id, year=year, guild_id=guild_id, offset=offset
    )
    await interaction.response.edit_message(embed=view._embed(), view=view)
    _bind_view_message(view, msg)


class StartTadokuGateView(_OwnerView):
    """Ask for Tadoku before challenge pick; skip OK (honor / link later)."""

    def __init__(
        self,
        owner_id: int,
        *,
        year: int,
        guild_id: str | None,
        offset: str,
    ) -> None:
        super().__init__(owner_id)
        self.year = year
        self.guild_id = guild_id
        self.offset = offset

    def _embed(self) -> discord.Embed:
        return _start_embed(
            title="Link Tadoku",
            description=(
                f"Timezone **`{self.offset}`** · **{_day_label(self.offset, 0)}**\n\n"
                "Most challenges verify immersion via **Tadoku**.\n"
                "Link your account before picking a challenge.\n\n"
                "Honor-only challenges don't need it — **Skip** is fine.\n"
                "You can also `/challenge link` later."
            ),
        )

    @discord.ui.button(label="Link Tadoku…", style=discord.ButtonStyle.primary, row=0)
    async def link_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            LinkModal(
                resume_message=interaction.message,
                resume_view=self,
                year=self.year,
                guild_id=self.guild_id,
                owner_id=self.owner_id,
            )
        )

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        view = TierPickView(
            self.owner_id, year=self.year, guild_id=self.guild_id
        )
        msg = _release_view(self) or interaction.message
        await interaction.response.edit_message(
            embed=_start_tier_embed(
                year=self.year,
                note=(
                    f"Timezone **`{self.offset}`** · Tadoku not linked "
                    f"(link later if needed — Tadoku challenges will ask again)"
                ),
            ),
            view=view,
        )
        _bind_view_message(view, msg)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=_start_embed(
                title="Cancelled", description="No challenge started."
            ),
            view=None,
        )


class StartTimezoneGateView(_OwnerView):
    """First step of /challenge start: confirm timezone, then Tadoku / difficulty."""

    def __init__(self, owner_id: int, *, year: int, guild_id: str | None) -> None:
        super().__init__(owner_id)
        self.year = year
        self.guild_id = guild_id
        self.offset = _default_offset_for(owner_id)
        self._rebuild()

    def _embed(self) -> discord.Embed:
        today = _day_label(self.offset, 0)
        return _start_embed(
            title="Your timezone",
            description=(
                f"Pick the UTC offset your clock shows **right now**.\n"
                f"Saved: **`{self.offset}`** → **{today}**\n\n"
                "This makes **Today** and schedule times correct.\n"
                "Then: Tadoku (if needed) → Difficulty → Challenge → Now or Schedule."
            ),
        )

    def _rebuild(self) -> None:
        self.clear_items()
        sel = discord.ui.Select(
            placeholder="Timezone (UTC offset now)",
            options=_offset_options(self.offset),
            row=0,
        )

        async def on_off(interaction: discord.Interaction) -> None:
            self.offset = _normalize_offset(sel.values[0])
            att_svc.set_tz_offset(
                str(self.owner_id),
                self.offset,
                interaction.user.display_name,
            )
            self._rebuild()
            await interaction.response.edit_message(embed=self._embed(), view=self)

        sel.callback = on_off  # type: ignore[method-assign]
        self.add_item(sel)

        cont = discord.ui.Button(
            label="Continue",
            style=discord.ButtonStyle.success,
            row=1,
        )
        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=1,
        )

        async def on_cont(interaction: discord.Interaction) -> None:
            att_svc.set_tz_offset(
                str(self.owner_id),
                self.offset,
                interaction.user.display_name,
            )
            await _advance_start_after_timezone(
                interaction,
                owner_id=self.owner_id,
                year=self.year,
                guild_id=self.guild_id,
                offset=self.offset,
                prev_view=self,
            )

        async def on_cancel(interaction: discord.Interaction) -> None:
            self.stop()
            await interaction.response.edit_message(
                embed=_start_embed(
                    title="Cancelled",
                    description="No challenge started.",
                ),
                view=None,
            )

        cont.callback = on_cont  # type: ignore[method-assign]
        cancel.callback = on_cancel  # type: ignore[method-assign]
        self.add_item(cont)
        self.add_item(cancel)


@challenge_group.command(name="start", description="Start or schedule a challenge")
async def challenge_start(interaction: discord.Interaction) -> None:
    # Long concurrent challenges may run beside others; start_attempt enforces rules.
    year = _catalog_year()
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    att_svc.ensure_user(str(interaction.user.id), interaction.user.display_name)
    view = StartTimezoneGateView(
        interaction.user.id, year=year, guild_id=guild_id
    )
    await interaction.response.send_message(
        embed=view._embed(),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


@challenge_group.command(name="list", description="Browse challenges by difficulty")
async def challenge_list(interaction: discord.Interaction) -> None:
    year = _catalog_year()
    view = ListTierView(interaction.user.id, year=year)
    await interaction.response.send_message(
        embed=discord.Embed(
            title=f"Challenges {year}",
            description=(
                f"Pick a difficulty to browse.\n{_cleared_note()}"
            ),
            color=0x5865F2,
        ),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


@challenge_group.command(
    name="status",
    description="Challenge status for you or another member",
)
@app_commands.describe(user="Member to check (default: you)")
async def challenge_status(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
) -> None:
    """Self: private card + actions. Other member: public mid-challenge card."""
    b = _bot_helpers()
    target = user or interaction.user
    self_view = target.id == interaction.user.id
    # Own status stays private; looking someone up is visible in-channel
    await interaction.response.defer(ephemeral=self_view)

    tid = str(target.id)
    actives = att_svc.active_attempts(tid)
    scheduleds = att_svc.scheduled_attempts(tid)
    active = actives[0] if actives else None
    scheduled = scheduleds[0] if scheduleds else None
    recent = att_svc.list_attempts(discord_id=tid, limit=8) if self_view else []
    lines: list[str] = []
    files: list[discord.File] = []
    card_kind = "status"
    open_ids = {int(a["id"]) for a in actives + scheduleds}

    if len(actives) + len(scheduleds) > 1 and self_view:
        bits = []
        for a in actives + scheduleds:
            conc = (
                " · concurrent"
                if att_svc.challenge_allows_concurrent(a)
                else ""
            )
            focus = att_svc.display_content(a)
            focus_s = f" · _{focus}_" if focus else ""
            if a.get("status") == "scheduled":
                when = b._ts(a["started_at"])
                bits.append(
                    f"`#{a['id']}` scheduled **{a['title']}**{focus_s}{conc} "
                    f"(starts <t:{when}:R>)"
                )
            else:
                ends = b._ts(a["ends_at"])
                snap = prog_svc.progress_snapshot(a)
                prog_s = f" · **{snap['pct']}%**" if snap else ""
                bits.append(
                    f"`#{a['id']}` active **{a['title']}**{focus_s}{conc}{prog_s} "
                    f"(ends <t:{ends}:R>)"
                )
        lines.append("**Open challenges**\n" + "\n".join(bits))
        lines.append(
            "Confirm/abandon: `/challenge confirm attempt_id:` or "
            "`/challenge abandon attempt_id:`"
        )
        if active:
            ends = b._ts(active["ends_at"])
            prog_line = prog_svc.card_progress_secondary(active)
            png, caption = await build_event_card(
                kind="active",
                user=target,
                title=active["title"],
                tier=active["tier"],
                number=int(active["number"]),
                param=active.get("param"),
                content=att_svc.display_content(active),
                ends_at_ts=ends,
                detail=b.confirm_howto(active, attempt_id=int(active["id"])),
                line_secondary=prog_line or None,
                media_filter=active.get("media_filter"),
            )
            files.append(as_discord_file(png, "active-card.png"))
            card_kind = "active"
    elif active:
        ends = b._ts(active["ends_at"])
        detail = (
            b.confirm_howto(active, attempt_id=int(active["id"]))
            if self_view
            else f"Ends <t:{ends}:R>"
        )
        prog_line = prog_svc.card_progress_secondary(active)
        png, caption = await build_event_card(
            kind="active",
            user=target,
            title=active["title"],
            tier=active["tier"],
            number=int(active["number"]),
            param=active.get("param"),
            content=att_svc.display_content(active),
            ends_at_ts=ends,
            detail=detail,
            line_secondary=prog_line or None,
            media_filter=active.get("media_filter"),
        )
        files.append(as_discord_file(png, "active-card.png"))
        card_kind = "active"
        if self_view:
            lines.append(
                f"{caption}\n{b.confirm_howto(active, attempt_id=int(active['id']))}"
            )
        else:
            lines.append(caption)
    elif scheduled:
        when = b._ts(scheduled["started_at"])
        ends = b._ts(scheduled["ends_at"])
        png, caption = await build_event_card(
            kind="scheduled",
            user=target,
            title=scheduled["title"],
            tier=scheduled["tier"],
            number=int(scheduled["number"]),
            param=scheduled.get("param"),
            content=att_svc.display_content(scheduled),
            starts_at_ts=when,
            ends_at_ts=ends,
            media_filter=scheduled.get("media_filter"),
        )
        files.append(as_discord_file(png, "scheduled-card.png"))
        card_kind = "scheduled"
        if self_view:
            lines.append(f"{caption}\n`/challenge abandon` to cancel.")
        else:
            lines.append(caption)
    else:
        if self_view:
            lines.append("**No open challenge.** Use `/challenge start`.")
        else:
            lines.append(
                f"**{target.display_name}** has no active or scheduled challenge."
            )

    if self_view:
        past = []
        for a in recent:
            if int(a["id"]) in open_ids:
                continue
            content = att_svc.display_content(a)
            focus = f" · _{content}_" if content else ""
            past.append(
                f"`{a['status']}` {a['tier']} #{a['number']} {a['title']}{focus}"
            )
        if past:
            lines.append("**Recent**\n" + "\n".join(past[:6]))

        db_user = att_svc.get_user(tid)
        tadoku = (
            f"Tadoku: `{db_user['tadoku_user_id']}`"
            if db_user and db_user.get("tadoku_user_id")
            else "Tadoku: **not linked** — `/challenge link`"
        )
        lines.append(tadoku)

    view = None
    # Single open → action buttons; multi → use attempt_id on slash cmds
    if self_view and len(actives) + len(scheduleds) == 1:
        only = active or scheduled
        view = StatusActionsView(
            interaction.user.id,
            has_active=bool(active),
            attempt_id=int(only["id"]) if only else None,
            attempt=only if active else None,
        )

    kwargs: dict = {
        "content": "\n".join(lines)[:1900],
        "ephemeral": self_view,
    }
    if files:
        kwargs["files"] = files
        kwargs["embeds"] = files_as_image_embeds(files, kind=card_kind)
    if view:
        kwargs["view"] = view
    await interaction.followup.send(**kwargs)
    if view:
        view.message = await interaction.original_response()


@challenge_group.command(
    name="active",
    description="List active and scheduled challenges",
)
@app_commands.describe(user="Member to check (default: you)")
async def challenge_active(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
) -> None:
    b = _bot_helpers()
    target = user or interaction.user
    self_view = target.id == interaction.user.id
    opens = att_svc.open_attempts(str(target.id))
    if not opens:
        msg = (
            "No open challenges. Use `/challenge start`."
            if self_view
            else f"**{target.display_name}** has no open challenges."
        )
        await interaction.response.send_message(msg, ephemeral=True)
        return

    lines = [f"**Open challenges** — {target.display_name}"]
    for a in opens:
        focus = att_svc.display_content(a)
        focus_s = f" · _{focus}_" if focus else ""
        conc = (
            " · concurrent"
            if att_svc.challenge_allows_concurrent(a)
            else ""
        )
        if a.get("status") == "scheduled":
            when = b._ts(a["started_at"])
            lines.append(
                f"`#{a['id']}` scheduled **{a['title']}**"
                f"{focus_s}{conc} (starts <t:{when}:R>)"
            )
        else:
            ends = b._ts(a["ends_at"])
            lines.append(
                f"`#{a['id']}` active **{a['title']}**"
                f"{focus_s}{conc} (ends <t:{ends}:R>)"
            )
    if self_view and len(opens) > 1:
        lines.append(
            "Use `attempt_id` on `/challenge confirm` / `/challenge abandon`."
        )
    await interaction.response.send_message(
        "\n".join(lines)[:1900], ephemeral=True
    )


@challenge_group.command(
    name="confirm",
    description="Confirm active challenge (Tadoku check runs immediately)",
)
@app_commands.describe(
    attempt_id="Attempt id when you have more than one open challenge",
)
async def challenge_confirm(
    interaction: discord.Interaction,
    attempt_id: Optional[int] = None,
) -> None:
    uid = str(interaction.user.id)
    picked = att_svc.resolve_user_attempt(
        uid, attempt_id=attempt_id, statuses=("active",)
    )
    if not picked.get("ok"):
        # scheduled-only path
        if attempt_id is None and not att_svc.active_attempts(uid):
            scheduleds = att_svc.scheduled_attempts(uid)
            if scheduleds:
                b = _bot_helpers()
                if len(scheduleds) == 1:
                    when = b._ts(scheduleds[0]["started_at"])
                    await interaction.response.send_message(
                        f"**{scheduleds[0]['title']}** starts <t:{when}:R> — "
                        "can’t confirm yet.",
                        ephemeral=True,
                    )
                    return
        await interaction.response.send_message(
            picked.get("error") or "No active challenge.",
            ephemeral=True,
        )
        return
    await _begin_confirm(interaction, picked["attempt"])


@challenge_group.command(
    name="content",
    description="Set or clean content labels on your attempt (incl. failed)",
)
@app_commands.describe(
    content="Replace content label (what you completed)",
    remove="Comma-separated series to strip from current content",
    param="Set Tadoku focus filter (active/scheduled)",
    clear_param="Clear Tadoku focus filter",
    attempt_id="Attempt id (default: latest)",
)
async def challenge_content(
    interaction: discord.Interaction,
    content: str = "",
    remove: str = "",
    param: str = "",
    clear_param: bool = False,
    attempt_id: Optional[int] = None,
) -> None:
    if not content and not remove and not param and not clear_param:
        await interaction.response.send_message(
            "Provide `content`, `remove`, `param`, and/or `clear_param`.",
            ephemeral=True,
        )
        return
    result = att_svc.user_update_attempt_content(
        str(interaction.user.id),
        attempt_id=attempt_id,
        content=content if content else None,
        remove=remove or None,
        param=param if param else None,
        clear_param=clear_param,
    )
    if not result.get("ok"):
        await interaction.response.send_message(
            result.get("error") or "Update failed.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"Updated **{result.get('title')}** `#{result.get('attempt_id')}`\n"
        f"content: `{result.get('content') or '—'}`\n"
        f"focus filter: `{result.get('param') or '—'}`",
        ephemeral=True,
    )


@challenge_group.command(
    name="failoverride",
    description="Honor-complete when Tadoku fails (no admin) — use after a failed confirm",
)
@app_commands.describe(
    content="What you completed (series / URL / works) — required",
    note="Why override (optional)",
    attempt_id="Attempt id when you have more than one active",
)
async def challenge_failoverride(
    interaction: discord.Interaction,
    content: str,
    note: str = "",
    attempt_id: Optional[int] = None,
) -> None:
    content = (content or "").strip()
    if not content:
        await interaction.response.send_message(
            "`content` is required (what you finished).",
            ephemeral=True,
        )
        return
    # Open as confirm with honor filter so rewards/announce share one path
    await interaction.response.defer(ephemeral=True)
    await _run_confirm(
        interaction,
        content=content,
        note=(note or "").strip() or None,
        filter_mode="honor",
        attempt_id=attempt_id,
    )


@challenge_group.command(
    name="match",
    description="Link misspelled Tadoku titles or specific log ids to your attempt",
)
@app_commands.describe(
    aliases="Comma-separated alternate titles (e.g. OnePiecee, ワンピース)",
    log_ids="Comma-separated Tadoku log ids to force-include",
    clear="Clear saved aliases and log ids",
    attempt_id="Attempt id (default: latest open)",
)
async def challenge_match(
    interaction: discord.Interaction,
    aliases: str = "",
    log_ids: str = "",
    clear: bool = False,
    attempt_id: Optional[int] = None,
) -> None:
    if not clear and not aliases and not log_ids:
        await interaction.response.send_message(
            "Provide `aliases`, `log_ids`, and/or `clear`. "
            "Use `/challenge logs` to copy ids.",
            ephemeral=True,
        )
        return
    result = att_svc.user_update_attempt_content(
        str(interaction.user.id),
        attempt_id=attempt_id,
        aliases=aliases if aliases or clear else None,
        log_ids=log_ids if log_ids or clear else None,
        clear_fixup=clear,
    )
    if not result.get("ok"):
        await interaction.response.send_message(
            result.get("error") or "Update failed.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"Match fixup on **{result.get('title')}** `#{result.get('attempt_id')}`\n"
        f"aliases: `{', '.join(result.get('aliases') or []) or '—'}`\n"
        f"log ids: `{', '.join(result.get('log_ids') or []) or '—'}`",
        ephemeral=True,
    )


@challenge_group.command(
    name="logs",
    description="Show Tadoku logs in your active window (for match fixup)",
)
@app_commands.describe(attempt_id="Attempt id when you have several active")
async def challenge_logs(
    interaction: discord.Interaction,
    attempt_id: Optional[int] = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    result = await att_svc.preview_attempt_logs(
        str(interaction.user.id), attempt_id=attempt_id
    )
    if not result.get("ok"):
        await interaction.followup.send(
            result.get("error") or "No logs.", ephemeral=True
        )
        return
    att = result["attempt"]
    lines = [
        f"**{att.get('title')}** `#{att.get('id')}` · "
        f"{result.get('log_count', 0)} logs in window",
        f"Focus: `{result.get('param') or '—'}` · "
        f"aliases: `{', '.join(result.get('aliases') or []) or '—'}`",
    ]
    for row in result.get("logs") or []:
        amt = row.get("amount")
        unit = row.get("unit") or ""
        lines.append(
            f"`{row.get('id')}` · {row.get('series') or '—'} "
            f"({amt} {unit})".strip()
        )
    lines.append("Copy ids into `/challenge match log_ids:` then `/challenge confirm`.")
    await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)


@challenge_group.command(
    name="abandon", description="Abandon active or scheduled challenge"
)
@app_commands.describe(
    attempt_id="Attempt id when you have more than one open challenge",
)
async def challenge_abandon(
    interaction: discord.Interaction,
    attempt_id: Optional[int] = None,
) -> None:
    picked = att_svc.resolve_user_attempt(
        str(interaction.user.id),
        attempt_id=attempt_id,
        statuses=("active", "scheduled"),
    )
    if not picked.get("ok"):
        await interaction.response.send_message(
            picked.get("error") or "Nothing to abandon.",
            ephemeral=True,
        )
        return
    open_att = picked["attempt"]
    view = AbandonConfirmView(
        interaction.user.id, attempt_id=int(open_att["id"])
    )
    await interaction.response.send_message(
        f"Abandon **{open_att.get('title')}** "
        f"(`{open_att.get('status')}`, `#{open_att['id']}`)?",
        view=view,
        ephemeral=True,
    )


@challenge_group.command(name="link", description="Link your Tadoku account")
async def challenge_link(interaction: discord.Interaction) -> None:
    await interaction.response.send_modal(LinkModal())


class TimezonePickView(_OwnerView):
    """Save wall-clock UTC offset used for schedule 'Today' / local times."""

    def __init__(self, owner_id: int, current: str) -> None:
        super().__init__(owner_id)
        self.offset = _normalize_offset(current)
        self._add_select()

    def _add_select(self) -> None:
        self.clear_items()
        sel = discord.ui.Select(
            placeholder="Offset matching your clock right now",
            options=_offset_options(self.offset),
        )

        async def cb(interaction: discord.Interaction) -> None:
            self.offset = _normalize_offset(sel.values[0])
            att_svc.set_tz_offset(
                str(self.owner_id),
                self.offset,
                interaction.user.display_name,
            )
            today = _day_label(self.offset, 0)
            # Rebuild select with new default so Discord doesn't snap back
            self._add_select()
            await interaction.response.edit_message(
                content=(
                    f"Saved timezone **`{self.offset}`**.\n"
                    f"In that zone, **{today}**.\n"
                    "Schedule menus will default to this. "
                    "Change again below if needed."
                ),
                view=self,
            )

        sel.callback = cb  # type: ignore[method-assign]
        self.add_item(sel)


@challenge_group.command(
    name="timezone",
    description="Set your timezone offset for scheduling (Today / local times)",
)
async def challenge_timezone(interaction: discord.Interaction) -> None:
    # Discord does not expose user TZ; we store the offset you pick.
    cur = _default_offset_for(interaction.user.id)
    today = _day_label(cur, 0)
    view = TimezonePickView(interaction.user.id, cur)
    await interaction.response.send_message(
        content=(
            f"Current saved offset: **`{cur}`** → **{today}**.\n"
            "Pick the UTC offset your clock shows **right now** "
            "(US Pacific summer = UTC-7). Discord has no auto timezone."
        ),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


@challenge_group.command(
    name="leaderboard",
    description="Top points (then clears, fewer fails)",
)
async def challenge_leaderboard(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    rows = att_svc.top_completers(15)
    if not rows:
        await interaction.followup.send("No completions yet.")
        return
    uids = [str(r["discord_id"]) for r in rows]
    best_map = att_svc.highest_completed_tiers(uids)
    entries = []
    for i, r in enumerate(rows):
        av = None
        uid = str(r["discord_id"])
        name = r["display_name"] or uid
        if uid.isdigit():
            try:
                u = interaction.client.get_user(int(uid))
                if u is None:
                    u = await interaction.client.fetch_user(int(uid))
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
    png = await render_leaderboard_card(
        title="Challenge board",
        entries=entries,
        footer=(
            f"{len(entries)} ranked · by points · "
            "ota 4 · hard 3 · med 2 · easy 1"
        ),
    )
    lb = as_discord_file(png, "leaderboard-card.png")
    await interaction.followup.send(
        file=lb,
        embeds=files_as_image_embeds([lb], kind="leaderboard"),
    )


@challenge_group.command(
    name="userinfo",
    description="Profile: active challenges, clears, points, rank",
)
@app_commands.describe(user="Member to look up (default: you)")
async def challenge_userinfo(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
) -> None:
    target = user or interaction.user
    await interaction.response.defer()
    tid = str(target.id)
    standing = att_svc.leaderboard_standing(tid)
    counts = att_svc.completion_counts(tid)
    best = att_svc.highest_completed_tier(tid)
    fails = att_svc.failure_count(tid)
    db_user = att_svc.get_user(tid)
    tadoku_linked = bool(db_user and db_user.get("tadoku_user_id"))
    opens = att_svc.open_attempts(tid)
    active = []
    for a in opens:
        row = {
            "title": a.get("title") or "?",
            "tier": a.get("tier") or "",
            "number": a.get("number"),
            "status": a.get("status") or "active",
        }
        snap = prog_svc.progress_snapshot(a)
        if snap:
            row["progress_pct"] = snap["pct"]
            row["progress_label"] = snap["label"]
        active.append(row)
    done = att_svc.list_attempts(discord_id=tid, status="completed", limit=100)
    completed = [
        {
            "title": a.get("title") or "?",
            "tier": a.get("tier") or "",
            "number": a.get("number"),
            "content": att_svc.display_content(a),
        }
        for a in done
    ]
    # list_attempts is newest-first; show oldest→newest on the card
    completed.reverse()
    av = await avatar_bytes(target)
    png = await render_userinfo_card(
        display_name=target.display_name,
        avatar=av,
        points=int(standing["points"]),
        clears=int(counts.get("total") or 0),
        rank=standing.get("rank"),
        ranked=int(standing.get("ranked") or 0),
        best_tier=best,
        by_tier=counts,
        fails=fails,
        active=active,
        completed=completed,
        tadoku_linked=tadoku_linked,
    )
    f = as_discord_file(png, "userinfo-card.png")
    bits = [
        f"**{target.display_name}**",
        f"Points **{standing['points']}** · Clears **{counts.get('total', 0)}**",
    ]
    if standing.get("rank"):
        bits.append(f"Rank **#{standing['rank']}** / {standing['ranked']}")
    if best:
        bits.append(f"Best tier **{best.replace('_', '-')}**")
    if active:
        n_act = sum(1 for a in active if a.get("status") != "scheduled")
        n_sch = len(active) - n_act
        open_bits = []
        if n_act:
            open_bits.append(f"Active **{n_act}**")
        if n_sch:
            open_bits.append(f"Scheduled **{n_sch}**")
        bits.append(" · ".join(open_bits))
    await interaction.followup.send(
        content=" · ".join(bits),
        file=f,
        embeds=files_as_image_embeds([f], kind="userinfo"),
    )


@challenge_group.command(
    name="admin",
    description="Admin tools (buttons) for a member’s attempt",
)
@app_commands.describe(user="Target member (default: you)")
async def challenge_admin(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
) -> None:
    b = _bot_helpers()
    if not b.is_bot_admin(interaction):
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return
    target = user or interaction.user
    view = AdminPanelView(interaction.user.id, target, interaction.guild)
    await interaction.response.send_message(
        f"Admin tools for {target.mention}\n"
        "Use the buttons — dry-run first, APPLY only when sure.",
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


async def _start_catalog_admin(
    interaction: discord.Interaction, *, mode: str
) -> None:
    if not _admin_only(interaction):
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return
    year = _catalog_year()
    titles = {
        "add": ("Admin · Add challenge", "Pick a **difficulty** for the new challenge."),
        "edit": ("Admin · Edit challenge", "Pick a **difficulty**, then the challenge to edit."),
        "remove": (
            "Admin · Remove challenge",
            "Pick a **difficulty**, then the challenge to delete from the catalog.",
        ),
    }
    title, desc = titles[mode]
    view = AdminCatalogTierView(interaction.user.id, year=year, mode=mode)
    await interaction.response.send_message(
        embed=_catalog_embed(title=title, description=desc),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


@challenge_group.command(
    name="add",
    description="Admin: add a challenge to the catalog",
)
async def challenge_add(interaction: discord.Interaction) -> None:
    await _start_catalog_admin(interaction, mode="add")


@challenge_group.command(
    name="edit",
    description="Admin: edit a catalog challenge",
)
async def challenge_edit(interaction: discord.Interaction) -> None:
    await _start_catalog_admin(interaction, mode="edit")


@challenge_group.command(
    name="remove",
    description="Admin: remove a challenge from the catalog",
)
async def challenge_remove(interaction: discord.Interaction) -> None:
    await _start_catalog_admin(interaction, mode="remove")


@challenge_group.command(
    name="testsched",
    description="Admin: try the schedule UI without creating an attempt",
)
async def challenge_testsched(interaction: discord.Interaction) -> None:
    """Walk Now / Day / Exact time flows; confirm is a dry-run only."""
    b = _bot_helpers()
    if not b.is_bot_admin(interaction):
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return
    year = _catalog_year()
    # Prefer a no-param challenge so focus modal is skipped
    items = list_challenges(year=year)
    ch = next((c for c in items if not needs_focus_param(c)), None)
    if ch is None and items:
        ch = items[0]
    if not ch:
        await interaction.response.send_message(
            "No challenges in catalog to preview.", ephemeral=True
        )
        return
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    focus = f"(test focus)" if needs_focus_param(ch) else None
    view = WhenPickView(
        interaction.user.id,
        ch=ch,
        focus=focus,
        year=year,
        guild_id=guild_id,
        tier=str(ch.get("tier") or "easy"),
        dry_run=True,
    )
    await interaction.response.send_message(
        embed=view._when_embed(),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


@challenge_group.command(
    name="testcards",
    description="Admin: preview every challenge card format",
)
@app_commands.describe(
    public="If true, post in this channel (default: only you can see)",
)
async def challenge_testcards(
    interaction: discord.Interaction,
    public: bool = False,
) -> None:
    b = _bot_helpers()
    if not b.is_bot_admin(interaction):
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=not public)
    av = await avatar_bytes(interaction.user)
    cards = await render_all_demo_cards(
        display_name=interaction.user.display_name,
        avatar=av,
    )
    # Discord allows max 10 files per message; we have 9.
    files = [
        as_discord_file(png, f"card_{kind}.png") for kind, png in cards[:10]
    ]
    labels = ", ".join(kind for kind, _ in cards)
    await interaction.followup.send(
        content=f"**Card formats** ({len(files)}): `{labels}`",
        files=files,
        embeds=files_as_image_embeds(files),
        ephemeral=not public,
    )


@challenge_group.command(
    name="testchallenger",
    description="Admin: preview unified start card (NC + challenge meta)",
)
@app_commands.describe(
    public="If true, post in this channel (default: only you can see)",
)
async def challenge_testchallenger(
    interaction: discord.Interaction,
    public: bool = False,
) -> None:
    b = _bot_helpers()
    if not b.is_bot_admin(interaction):
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=not public)
    templates = list_templates()
    if not templates:
        await interaction.followup.send(
            "No templates in `new_challenger/`.",
            ephemeral=not public,
        )
        return
    import time

    ends = int(time.time()) + 86400
    files = await _layout_e_start_files(
        user=interaction.user,
        kind="start",
        title="Challenger preview",
        tier="hard",
        number=1,
        focus="preview",
        ends_at_ts=ends,
        duration_hours=24,
        include_challenger=True,
    )
    if not files:
        await interaction.followup.send(
            "Failed to render unified start card (check templates/hole mask).",
            ephemeral=not public,
        )
        return
    await interaction.followup.send(
        content=(
            f"**Unified start** preview for {interaction.user.mention}\n"
            f"Templates: `{len(templates)}` · one card"
        ),
        files=files,
        embeds=files_as_image_embeds(files, chrome=False),
        ephemeral=not public,
    )


@challenge_group.command(
    name="testcongrats",
    description="Admin: preview a completion congrats image (private)",
)
@app_commands.describe(
    tier="Tier pool to prefer (default: easy)",
    content="Focus title for content-matched pics (optional)",
    mark="Count toward uniqueness tracking (default: off)",
)
@app_commands.choices(
    tier=[
        app_commands.Choice(name="Easy", value="easy"),
        app_commands.Choice(name="Medium", value="medium"),
        app_commands.Choice(name="Hard", value="hard"),
        app_commands.Choice(name="Ota-Master", value="ota_master"),
    ]
)
async def challenge_testcongrats(
    interaction: discord.Interaction,
    tier: str = "easy",
    content: str = "",
    mark: bool = False,
) -> None:
    b = _bot_helpers()
    if not b.is_bot_admin(interaction):
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    path = pick_congrats(tier=tier, content=content or None, mark=mark)
    if path is None:
        await interaction.followup.send(
            "No congrats image found under `completion_pictures/` "
            f"(tier=`{tier}`, content=`{content or '—'}`).",
            ephemeral=True,
        )
        return
    # Also show the completion card so admins see the full announce pair
    png, caption = await build_event_card(
        kind="complete",
        user=interaction.user,
        title="Congrats preview",
        tier=tier,
        number=0,
        content=content or None,
        method="test",
        detail="preview only",
    )
    card = as_discord_file(png, "complete-card.png")
    cong = as_discord_framed_congrats(
        path, tier=tier, title="Congrats preview", number=0
    )
    await interaction.followup.send(
        content=(
            f"{caption}\n"
            f"Congrats file: `{path.name}`\n"
            f"tier=`{tier}` content=`{content or '—'}` mark=`{mark}`"
        ),
        files=[card],
        embeds=files_as_image_embeds([card], chrome=False),
        ephemeral=True,
    )
    await interaction.followup.send(
        files=[cong],
        embeds=files_as_image_embeds([cong], chrome=False),
        ephemeral=True,
    )
