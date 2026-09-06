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
from app.congrats import as_discord_congrats, pick_congrats
from app.db import db_session
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

# Common offsets for schedule picker (label, value for parse_schedule_local)
OFFSET_OPTIONS = [
    ("UTC", "+00:00"),
    ("UTC−5 (US East)", "-05:00"),
    ("UTC−6 (US Central)", "-06:00"),
    ("UTC−7 (US Mountain)", "-07:00"),
    ("UTC−8 (US Pacific)", "-08:00"),
    ("UTC+0 / London", "+00:00"),
    ("UTC+1 (CET)", "+01:00"),
    ("UTC+2", "+02:00"),
    ("UTC+8", "+08:00"),
    ("UTC+9 (Japan/Korea)", "+09:00"),
    ("UTC+10", "+10:00"),
    ("UTC+5:30 (India)", "+05:30"),
]


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
    png, caption = await build_event_card(
        kind=kind,
        user=interaction.user,
        title=ch["title"],
        tier=ch["tier"],
        number=int(ch["number"]),
        param=result.get("param"),
        starts_at_ts=start_ts if kind == "scheduled" else None,
        ends_at_ts=ends,
        media_filter=media,
    )
    howto = b.confirm_howto(ch) if kind == "start" else "Cancel anytime with `/challenge abandon`."
    card = as_discord_file(png, f"{kind}-card.png")
    await interaction.followup.send(
        content=f"{caption}\n{howto}",
        file=card,
        embeds=files_as_image_embeds([card], kind=kind),
        ephemeral=True,
    )
    await announce(
        interaction.client,
        kind=kind,
        guild=interaction.guild,
        user=interaction.user,
        title=ch["title"],
        tier=ch["tier"],
        number=int(ch["number"]),
        param=result.get("param"),
        starts_at_ts=start_ts if kind == "scheduled" else None,
        ends_at_ts=ends,
        media_filter=media,
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
        await interaction.response.send_message(
            embed=_start_embed(
                title="When do you want to start?",
                description=(
                    f"**{self.ch['tier']} #{self.ch['number']}: {self.ch['title']}**\n"
                    f"Focus: **{focus}**\n\n"
                    "Start **now**, or **schedule** with the day/time menus."
                ),
                ch=self.ch,
            ),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()


def _resolve_schedule_from_picks(
    offset: str, day_rel: int, hhmm: str
) -> datetime:
    """day_rel=0 today in that offset, 1=tomorrow… + HH:MM local → UTC."""
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
    local_tz = timezone(delta)
    local_now = datetime.now(timezone.utc).astimezone(local_tz)
    target_date = (local_now + timedelta(days=int(day_rel))).date()
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


class SchedulePickView(_OwnerView):
    def __init__(
        self,
        owner_id: int,
        *,
        ch: dict,
        focus: str | None,
        guild_id: str | None,
        year: int,
        tier: str,
    ) -> None:
        super().__init__(owner_id)
        self.ch = ch
        self.focus = focus
        self.guild_id = guild_id
        self.year = year
        self.tier = tier
        self.offset = "+00:00"
        self.day_rel = 0
        self.hhmm = "18:00"
        self.add_item(self._offset_select())
        self.add_item(self._day_select())
        self.add_item(self._time_select())

    def _offset_select(self) -> discord.ui.Select:
        seen = set()
        opts = []
        for label, val in OFFSET_OPTIONS:
            if val in seen:
                continue
            seen.add(val)
            opts.append(discord.SelectOption(label=label[:100], value=val, default=val == "+00:00"))
        sel = discord.ui.Select(
            placeholder="Your timezone (UTC offset)",
            options=opts[:25],
            row=0,
        )

        async def cb(interaction: discord.Interaction) -> None:
            self.offset = sel.values[0]
            await interaction.response.defer()

        sel.callback = cb  # type: ignore[method-assign]
        return sel

    def _day_select(self) -> discord.ui.Select:
        opts = []
        for i in range(0, 14):
            if i == 0:
                lab = "Today"
            elif i == 1:
                lab = "Tomorrow"
            else:
                lab = f"In {i} days"
            opts.append(
                discord.SelectOption(label=lab, value=str(i), default=i == 0)
            )
        sel = discord.ui.Select(placeholder="Day", options=opts, row=1)

        async def cb(interaction: discord.Interaction) -> None:
            self.day_rel = int(sel.values[0])
            await interaction.response.defer()

        sel.callback = cb  # type: ignore[method-assign]
        return sel

    def _time_select(self) -> discord.ui.Select:
        opts = []
        for h in range(0, 24):
            for m in (0, 30):
                hhmm = f"{h:02d}:{m:02d}"
                opts.append(
                    discord.SelectOption(
                        label=hhmm,
                        value=hhmm,
                        default=hhmm == "18:00",
                    )
                )
        # Discord max 25 options — keep morning/evening spread
        # Prefer every hour + half for common window: use 25 slots from 6:00
        opts = []
        for h in range(6, 24):
            for m in (0, 30):
                hhmm = f"{h:02d}:{m:02d}"
                opts.append(
                    discord.SelectOption(
                        label=hhmm,
                        value=hhmm,
                        default=hhmm == "18:00",
                    )
                )
                if len(opts) >= 25:
                    break
            if len(opts) >= 25:
                break
        # ensure late-night covered: replace last with 00:00/03:00 if needed — 6-18 covers day
        sel = discord.ui.Select(placeholder="Local time", options=opts, row=2)

        async def cb(interaction: discord.Interaction) -> None:
            self.hhmm = sel.values[0]
            await interaction.response.defer()

        sel.callback = cb  # type: ignore[method-assign]
        return sel

    @discord.ui.button(label="Confirm schedule", style=discord.ButtonStyle.success, row=3)
    async def confirm_sched(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            starts = _resolve_schedule_from_picks(self.offset, self.day_rel, self.hhmm)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if starts <= datetime.now(timezone.utc):
            await interaction.response.send_message(
                "That time is already past — pick a later day/time.",
                ephemeral=True,
            )
            return
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            embed=_start_embed(
                title="Scheduling…",
                description=f"**{self.ch['title']}** · <t:{int(starts.timestamp())}:F>",
                ch=self.ch,
            ),
            view=self,
        )
        await _finalize_start(
            interaction,
            challenge_id=self.ch["id"],
            focus=self.focus,
            guild_id=self.guild_id,
            starts_at=starts,
        )

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=3)
    async def back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        view = WhenPickView(
            self.owner_id,
            ch=self.ch,
            focus=self.focus,
            year=self.year,
            guild_id=self.guild_id,
            tier=self.tier,
        )
        await interaction.response.edit_message(
            embed=_start_embed(
                title="When do you want to start?",
                description=f"**{self.ch['tier']} #{self.ch['number']}: {self.ch['title']}**",
                ch=self.ch,
            ),
            view=view,
        )
        view.message = interaction.message


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
    ) -> None:
        super().__init__(owner_id)
        self.ch = ch
        self.focus = focus
        self.year = year
        self.guild_id = guild_id
        self.tier = tier

    @discord.ui.button(label="Start now", style=discord.ButtonStyle.success, row=0)
    async def start_now(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
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
        view = SchedulePickView(
            self.owner_id,
            ch=self.ch,
            focus=self.focus,
            guild_id=self.guild_id,
            year=self.year,
            tier=self.tier,
        )
        await interaction.response.edit_message(
            embed=_start_embed(
                title="Pick day & time",
                description=(
                    f"**{self.ch['title']}**\n"
                    "1) Timezone offset  2) Day  3) Local time  4) **Confirm schedule**"
                ),
                ch=self.ch,
            ),
            view=view,
        )
        view.message = interaction.message

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
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
        await interaction.response.edit_message(
            embed=_start_embed(title="Cancelled", description="No challenge started."),
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
            embed=_start_embed(
                title="When do you want to start?",
                description=(
                    f"**{ch['tier']} #{ch['number']}: {ch['title']}**\n\n"
                    "Start **now**, or **schedule** a future day/time."
                ),
                ch=ch,
            ),
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
    view = TierPickView(interaction.user.id, year=year, guild_id=guild_id)
    await interaction.response.send_message(
        embed=_start_embed(
            description=(
                f"**{year}** catalog\n"
                "1️⃣ Difficulty → 2️⃣ Challenge → 3️⃣ Focus if needed → "
                "4️⃣ **Now** or **Schedule**"
            ),
        ),
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


@challenge_group.command(name="status", description="Your challenge status + actions")
async def challenge_status(interaction: discord.Interaction) -> None:
    b = _bot_helpers()
    await interaction.response.defer(ephemeral=True)
    active = att_svc.active_attempt(str(interaction.user.id))
    scheduled = att_svc.scheduled_attempt(str(interaction.user.id))
    recent = att_svc.list_attempts(discord_id=str(interaction.user.id), limit=8)
    lines: list[str] = []
    files: list[discord.File] = []

    if active:
        ends = b._ts(active["ends_at"])
        png, _ = await build_event_card(
            kind="active",
            user=interaction.user,
            title=active["title"],
            tier=active["tier"],
            number=int(active["number"]),
            param=active.get("param"),
            content=att_svc.display_content(active),
            ends_at_ts=ends,
            detail=b.confirm_howto(active),
            media_filter=active.get("media_filter"),
        )
        files.append(as_discord_file(png, "active-card.png"))
        lines.append(b.confirm_howto(active))
    elif scheduled:
        when = b._ts(scheduled["started_at"])
        ends = b._ts(scheduled["ends_at"])
        png, _ = await build_event_card(
            kind="scheduled",
            user=interaction.user,
            title=scheduled["title"],
            tier=scheduled["tier"],
            number=int(scheduled["number"]),
            param=scheduled.get("param"),
            starts_at_ts=when,
            ends_at_ts=ends,
            media_filter=scheduled.get("media_filter"),
        )
        files.append(as_discord_file(png, "scheduled-card.png"))
    else:
        lines.append("**No active challenge.** Use `/challenge start`.")

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

    user = att_svc.get_user(str(interaction.user.id))
    tadoku = (
        f"Tadoku: `{user['tadoku_user_id']}`"
        if user and user.get("tadoku_user_id")
        else "Tadoku: **not linked** — `/challenge link`"
    )
    lines.append(tadoku)

    view = None
    if active or scheduled:
        view = StatusActionsView(interaction.user.id, has_active=bool(active))

    kwargs: dict = {
        "content": "\n".join(lines)[:1900],
        "ephemeral": True,
    }
    if files:
        kwargs["files"] = files
        kwargs["embeds"] = files_as_image_embeds(
            files, kind="active" if active else "scheduled"
        )
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


@challenge_group.command(name="leaderboard", description="Top completers")
async def challenge_leaderboard(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    with db_session() as conn:
        rows = conn.execute(
            "SELECT a.discord_id, u.display_name, COUNT(*) AS n "
            "FROM attempts a LEFT JOIN users u ON u.discord_id=a.discord_id "
            "WHERE a.status='completed' GROUP BY a.discord_id "
            "ORDER BY n DESC LIMIT 15"
        ).fetchall()
    if not rows:
        await interaction.followup.send("No completions yet.")
        return
    entries = []
    for i, r in enumerate(rows):
        av = None
        uid = r["discord_id"]
        name = r["display_name"] or uid
        if str(uid).isdigit():
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
            {"rank": i + 1, "name": name, "score": int(r["n"]), "avatar": av}
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
        as_discord_congrats(path),
    ]
    await interaction.followup.send(
        content=(
            f"{caption}\n"
            f"Congrats file: `{path.name}`\n"
            f"tier=`{tier}` content=`{content or '—'}` mark=`{mark}`"
        ),
        files=files,
        embeds=files_as_image_embeds(files, kind="complete"),
        ephemeral=True,
    )
