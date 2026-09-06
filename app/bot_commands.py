"""Slash commands + Discord UI (buttons / selects / modals)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands

from app.catalog import get_challenge, list_challenges
from app.challenge_card import (
    as_discord_file,
    avatar_bytes,
    files_as_image_embeds,
    render_all_demo_cards,
    render_leaderboard_card,
)
from app.congrats import as_discord_framed_congrats, pick_congrats
from app.db import db_session
from app.new_challenger import (
    as_discord_new_challenger,
    list_templates,
    render_new_challenger,
)
from app.services import attempts as att_svc
from app.services.rewards import (
    announce,
    announce_completion,
    apply_rewards,
    build_event_card,
)

logger = logging.getLogger(__name__)


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
) -> list:
    """Start pair: new-challenger banner + full challenge card."""
    files = []
    av = await avatar_bytes(user)
    nc = render_new_challenger(av, tier=tier)
    if nc:
        files.append(as_discord_new_challenger(nc))
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
        )
    if png:
        files.append(as_discord_file(png, f"{kind}-card.png"))
    return files


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
    embed.set_footer(text="Only you can use these controls · times out in 3 min")
    return embed


async def _finalize_start(
    interaction: discord.Interaction,
    *,
    challenge_id: str,
    focus: str | None,
    guild_id: str | None,
    starts_at: datetime | None = None,
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
        await interaction.followup.send(result.get("error") or "Failed.", ephemeral=True)
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
    if kind == "scheduled":
        howto = "A new card posts when it goes live. Cancel: `/challenge abandon`."
    else:
        howto = b.confirm_howto(ch)
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
    kwargs: dict = {"content": f"{caption}\n{howto}", "ephemeral": True}
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
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    result = await att_svc.confirm_attempt(str(interaction.user.id), note=note)
    if not result.get("ok"):
        msg = result.get("error") or "Not completed."
        if result.get("progress"):
            msg += f"\nProgress: {result['progress']}"
        if result.get("log_count") is not None:
            msg += f"\nLogs seen in window: {result['log_count']}"
        await interaction.followup.send(msg, ephemeral=True)
        att = result.get("attempt")
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


async def _run_abandon(interaction: discord.Interaction) -> None:
    result = att_svc.abandon_attempt(str(interaction.user.id))
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
    def __init__(self, owner_id: int, *, timeout: float = 180) -> None:
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
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
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
            await interaction.response.edit_message(
                embed=view._when_embed(), view=view
            )
            view.message = interaction.message

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

    def _when_embed(self) -> discord.Embed:
        banner = (
            "**TEST MODE — nothing will be saved.**\n\n"
            if self.dry_run
            else ""
        )
        off = _default_offset_for(self.owner_id)
        today = _day_label(off, 0)
        return _start_embed(
            title=("TEST · " if self.dry_run else "") + "When do you want to start?",
            description=(
                f"{banner}"
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
        await interaction.response.edit_message(
            embed=view._schedule_embed(),
            view=view,
        )
        view.message = interaction.message

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
        pick = ChallengePickView(
            self.owner_id,
            tier=self.tier,
            year=self.year,
            guild_id=self.guild_id,
            challenges=items,
        )
        lines = [
            f"**#{c['number']} {c['title']}**"
            + (f" · needs *{c['param_label']}*" if c.get("param_label") else "")
            for c in items
        ]
        await interaction.response.edit_message(
            embed=_start_embed(
                title=f"{self.tier} challenges",
                description="Select one:\n" + "\n".join(lines),
            ),
            view=pick,
        )
        pick.message = interaction.message

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
    def __init__(self, challenges: list[dict]) -> None:
        options = []
        for c in challenges[:25]:
            need = c.get("param_label")
            desc = (c.get("description") or "")[:100]
            if need:
                desc = f"Needs {need} · {desc}"[:100]
            options.append(
                discord.SelectOption(
                    label=f"#{c['number']} · {c['title']}"[:100],
                    value=str(c["id"]),
                    description=desc or None,
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
        if ch.get("param_label"):
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
        await interaction.response.edit_message(
            embed=when._when_embed(),
            view=when,
        )
        when.message = interaction.message


class ChallengePickView(_OwnerView):
    def __init__(
        self,
        owner_id: int,
        *,
        tier: str,
        year: int,
        guild_id: str | None,
        challenges: list[dict],
    ) -> None:
        super().__init__(owner_id)
        self.tier = tier
        self.year = year
        self.guild_id = guild_id
        self.add_item(ChallengeSelect(challenges))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        view = TierPickView(self.owner_id, year=self.year, guild_id=self.guild_id)
        await interaction.response.edit_message(
            embed=_start_embed(
                description="Pick a **difficulty**, then a challenge from the list.",
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
        if view.mode == "list":
            b = _bot_helpers()
            lines = []
            for c in items:
                param = f" `<{c['param_label']}>`" if c.get("param_label") else ""
                lines.append(
                    f"**#{c['number']} {c['title']}**{param} — {c.get('description') or ''} "
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
            need = f" · needs *{c['param_label']}*" if c.get("param_label") else ""
            lines.append(f"**#{c['number']} {c['title']}**{need}")
        pick = ChallengePickView(
            view.owner_id,
            tier=self.tier,
            year=view.year,
            guild_id=view.guild_id,
            challenges=items,
        )
        await interaction.response.edit_message(
            embed=_start_embed(
                title=f"{self.label} challenges",
                description="Select one:\n" + "\n".join(lines),
            ),
            view=pick,
        )
        pick.message = interaction.message


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
    def __init__(self, owner_id: int, *, has_active: bool) -> None:
        super().__init__(owner_id)
        if has_active:
            confirm = discord.ui.Button(
                label="Confirm complete",
                style=discord.ButtonStyle.success,
                row=0,
            )
            note = discord.ui.Button(
                label="Confirm + note…",
                style=discord.ButtonStyle.primary,
                row=0,
            )

            async def _confirm(interaction: discord.Interaction) -> None:
                await _run_confirm(interaction)

            async def _note(interaction: discord.Interaction) -> None:
                await interaction.response.send_modal(ConfirmNoteModal())

            confirm.callback = _confirm  # type: ignore[method-assign]
            note.callback = _note  # type: ignore[method-assign]
            self.add_item(confirm)
            self.add_item(note)

        abandon = discord.ui.Button(
            label="Abandon / cancel",
            style=discord.ButtonStyle.danger,
            row=1,
        )

        async def _abandon(interaction: discord.Interaction) -> None:
            view = AbandonConfirmView(self.owner_id)
            await interaction.response.send_message(
                "Really abandon your current challenge?",
                view=view,
                ephemeral=True,
            )

        abandon.callback = _abandon  # type: ignore[method-assign]
        self.add_item(abandon)


class ConfirmNoteModal(discord.ui.Modal, title="Confirm with note"):
    note = discord.ui.TextInput(
        label="Note (optional spirit / honor)",
        required=False,
        max_length=200,
        style=discord.TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _run_confirm(
            interaction,
            note=str(self.note.value).strip() or None,
        )


class ConfirmView(_OwnerView):
    @discord.ui.button(label="Confirm complete", style=discord.ButtonStyle.success)
    async def go(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await _run_confirm(interaction)

    @discord.ui.button(label="Add note…", style=discord.ButtonStyle.primary)
    async def note(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(ConfirmNoteModal())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class AbandonConfirmView(_OwnerView):
    @discord.ui.button(label="Yes, abandon", style=discord.ButtonStyle.danger)
    async def yes(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await _run_abandon(interaction)
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

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.user_id.value).strip()
        m = re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            raw,
            re.I,
        )
        if not m:
            await interaction.response.send_message(
                "Could not find a UUID. Paste your Tadoku user id or profile URL.",
                ephemeral=True,
            )
            return
        name = str(self.display_name.value).strip() or None
        att_svc.link_tadoku(str(interaction.user.id), m.group(0), name)
        await interaction.response.send_message(
            f"Linked Tadoku user `{m.group(0)}`.",
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


# ----- slash commands -----


class StartTimezoneGateView(_OwnerView):
    """First step of /challenge start: confirm timezone, then difficulty."""

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
                "Then: Difficulty → Challenge → Now or Schedule."
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
            # Persist current selection even if they only hit Continue
            att_svc.set_tz_offset(
                str(self.owner_id),
                self.offset,
                interaction.user.display_name,
            )
            view = TierPickView(
                self.owner_id, year=self.year, guild_id=self.guild_id
            )
            await interaction.response.edit_message(
                embed=_start_embed(
                    description=(
                        f"Timezone **`{self.offset}`** · **{_day_label(self.offset, 0)}**\n"
                        f"**{self.year}** catalog\n"
                        "1️⃣ Difficulty → 2️⃣ Challenge → 3️⃣ Focus if needed → "
                        "4️⃣ **Now** or **Schedule**"
                    ),
                ),
                view=view,
            )
            view.message = interaction.message

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
    open_att = att_svc.open_attempt(str(interaction.user.id))
    b = _bot_helpers()
    if open_att:
        if open_att.get("status") == "scheduled":
            when = b._ts(open_att["started_at"])
            await interaction.response.send_message(
                f"You already scheduled **{open_att['title']}** "
                f"(starts <t:{when}:R>). Use `/challenge abandon` to cancel.",
                ephemeral=True,
            )
            return
        ends = b._ts(open_att["ends_at"])
        await interaction.response.send_message(
            f"You already have **{open_att['title']}** (ends <t:{ends}:R>). "
            f"Abandon first if you want a new one.",
            ephemeral=True,
        )
        return
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
            description="Pick a difficulty to browse.",
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
    active = att_svc.active_attempt(tid)
    scheduled = att_svc.scheduled_attempt(tid)
    recent = att_svc.list_attempts(discord_id=tid, limit=8) if self_view else []
    lines: list[str] = []
    files: list[discord.File] = []
    card_kind = "status"

    if active:
        ends = b._ts(active["ends_at"])
        detail = b.confirm_howto(active) if self_view else f"Ends <t:{ends}:R>"
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
            media_filter=active.get("media_filter"),
        )
        files.append(as_discord_file(png, "active-card.png"))
        card_kind = "active"
        if self_view:
            lines.append(f"{caption}\n{b.confirm_howto(active)}")
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
        # caption already has viewer-local <t:F> / <t:R>
        if self_view:
            lines.append(f"{caption}\n`/challenge abandon` to cancel.")
        else:
            lines.append(caption)
    else:
        if self_view:
            lines.append("**No open challenge.** Use `/challenge start`.")
        else:
            lines.append(f"**{target.display_name}** has no active or scheduled challenge.")

    if self_view:
        past = []
        for a in recent:
            if active and a["id"] == active["id"]:
                continue
            if scheduled and a["id"] == scheduled["id"]:
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
    if self_view and (active or scheduled):
        view = StatusActionsView(interaction.user.id, has_active=bool(active))

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


@challenge_group.command(name="confirm", description="Confirm your active challenge")
async def challenge_confirm(interaction: discord.Interaction) -> None:
    active = att_svc.active_attempt(str(interaction.user.id))
    if not active:
        scheduled = att_svc.scheduled_attempt(str(interaction.user.id))
        if scheduled:
            b = _bot_helpers()
            when = b._ts(scheduled["started_at"])
            await interaction.response.send_message(
                f"**{scheduled['title']}** starts <t:{when}:R> — can’t confirm yet.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "No active challenge.", ephemeral=True
        )
        return
    b = _bot_helpers()
    view = ConfirmView(interaction.user.id)
    await interaction.response.send_message(
        f"Confirm **{active['tier']} #{active['number']}: {active['title']}**?\n"
        f"{b.confirm_howto(active)}",
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


@challenge_group.command(name="abandon", description="Abandon active or scheduled challenge")
async def challenge_abandon(interaction: discord.Interaction) -> None:
    open_att = att_svc.open_attempt(str(interaction.user.id))
    if not open_att:
        await interaction.response.send_message(
            "Nothing to abandon.", ephemeral=True
        )
        return
    view = AbandonConfirmView(interaction.user.id)
    await interaction.response.send_message(
        f"Abandon **{open_att.get('title')}** (`{open_att.get('status')}`)?",
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


@challenge_group.command(name="leaderboard", description="Top completers")
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
                "score": int(r["n"]),
                "avatar": av,
                "best_tier": best_map.get(uid),
            }
        )
    png = await render_leaderboard_card(
        title="Challenge clears",
        entries=entries,
        footer=f"{len(entries)} ranked · completed attempts",
    )
    lb = as_discord_file(png, "leaderboard-card.png")
    await interaction.followup.send(
        file=lb,
        embeds=files_as_image_embeds([lb], kind="leaderboard"),
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
    ch = next((c for c in items if not c.get("param_label")), None)
    if ch is None and items:
        ch = items[0]
    if not ch:
        await interaction.response.send_message(
            "No challenges in catalog to preview.", ephemeral=True
        )
        return
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    focus = f"(test focus)" if ch.get("param_label") else None
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
    description="Admin: preview start pair (NC banner + full challenge card)",
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
    files = await _layout_e_start_files(
        user=interaction.user,
        kind="start",
        title="Challenger preview",
        tier="hard",
        number=1,
        focus="preview",
        ends_at_ts=None,
    )
    if not files:
        await interaction.followup.send(
            "Failed to render start pair (check templates/hole mask).",
            ephemeral=not public,
        )
        return
    await interaction.followup.send(
        content=(
            f"**Start pair** preview for {interaction.user.mention}\n"
            f"Templates: `{len(templates)}` · NC banner + challenge card"
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
    files = [
        as_discord_file(png, "complete-card.png"),
        as_discord_framed_congrats(
            path, tier=tier, title="Congrats preview", number=0
        ),
    ]
    await interaction.followup.send(
        content=(
            f"{caption}\n"
            f"Congrats file: `{path.name}`\n"
            f"tier=`{tier}` content=`{content or '—'}` mark=`{mark}`"
        ),
        files=files,
        embeds=files_as_image_embeds(files, kind="complete", tier=tier),
        ephemeral=True,
    )
