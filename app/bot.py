from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from app.catalog import list_challenges
from app.config import get_settings, merge_runtime
from app.db import db_session, get_all_settings
from app.services import attempts as att_svc
from app.services.rewards import announce_completion, apply_rewards
from app.services.verify import parse_iso

logger = logging.getLogger(__name__)

TIER_CHOICES = [
    app_commands.Choice(name="Easy", value="easy"),
    app_commands.Choice(name="Medium", value="medium"),
    app_commands.Choice(name="Hard", value="hard"),
    app_commands.Choice(name="Ota-Master", value="ota_master"),
]


def _runtime():
    with db_session() as conn:
        return merge_runtime(get_settings(), get_all_settings(conn))


def _ts(iso: str) -> int:
    dt = parse_iso(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def confirm_howto(ch: dict) -> str:
    """Plain-English how this challenge is confirmed (shown on start/list/status)."""
    mode = (ch.get("verify") or "tadoku").lower()
    kind = (ch.get("goal_kind") or "").lower()
    amount = ch.get("goal_amount")
    media = ch.get("media_filter") or "any"
    param = ch.get("param_label")

    if mode == "honor":
        lines = [
            "**Confirmation: manual (honor system)**",
            "Tadoku is **not** checked for this challenge.",
            "When you finish, run `/challenge confirm` (optional note is fine).",
        ]
        if kind == "immersion_hours":
            lines.append(
                f"Goal: **{amount:g} hours** of immersion (any media you count). "
                "Track time yourself — the bot trusts your confirm."
            )
        elif kind == "complete_work":
            lines.append(
                "Goal: finish the full work end-to-end. "
                "Tadoku cannot prove ‘complete novel’, so confirm is manual."
            )
        else:
            lines.append("Complete the goal, then confirm yourself.")
        return "\n".join(lines)

    # tadoku (default)
    goal_bits = {
        "characters": f"≥ **{amount:,.0f}** characters on Tadoku",
        "minutes": f"≥ **{amount:g}** minutes on Tadoku",
        "volumes": f"≥ **{amount:g}** manga volumes (from Tadoku logs / page estimates)",
        "episodes": f"≥ **{amount:g}** anime episodes (from Tadoku logs)",
        "immersion_hours": f"≥ **{amount:g}** hours from timed Tadoku logs",
    }.get(kind, f"meet goal `{kind}` = {amount} via Tadoku")

    lines = [
        "**Confirmation: Tadoku-verified**",
        "`/challenge confirm` only succeeds if your Tadoku logs in this window meet the goal.",
        f"Goal check: {goal_bits} (media filter: `{media}`).",
        "1. Link once: `/challenge link` with your Tadoku user id/URL",
        "2. Log immersion on tadoku.app **during** the timer",
        "3. `/challenge confirm` — bot pulls logs and checks the numbers",
    ]
    if param:
        lines.append(
            f"Logs should mention your **{param}** focus so matching can filter them."
        )
    return "\n".join(lines)


def verify_label(ch: dict) -> str:
    mode = (ch.get("verify") or "tadoku").lower()
    if mode == "honor":
        return "manual"
    return "tadoku"


def is_bot_admin(interaction: discord.Interaction) -> bool:
    """Guild owner, Administrator permission, or ADMIN_ROLE_IDS (LucidDefender-style)."""
    if interaction.guild and interaction.user.id == interaction.guild.owner_id:
        return True
    if not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    rt = _runtime()
    allowed = set(rt.get("admin_role_ids") or []) | set(
        get_settings().admin_role_id_list()
    )
    if not allowed:
        return False
    member_roles = {str(r.id) for r in interaction.user.roles}
    return bool(member_roles & allowed)


class ChallengeBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)
        self.tree_synced = False

    async def setup_hook(self) -> None:
        self.tree.add_command(challenge_group)
        self.expire_loop.start()
        settings = get_settings()
        guild_id = settings.discord_guild_id or _runtime().get("guild_id")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Synced commands to guild %s", guild_id)
        else:
            await self.tree.sync()
            logger.info("Synced global commands")
        self.tree_synced = True

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user and self.user.id)

    @tasks.loop(seconds=60)
    async def expire_loop(self) -> None:
        n = att_svc.expire_overdue()
        if n:
            logger.info("Expired %s attempt(s)", n)

    @expire_loop.before_loop
    async def before_expire(self) -> None:
        await self.wait_until_ready()


challenge_group = app_commands.Group(
    name="challenge",
    description="Yearly immersion challenges",
)


@challenge_group.command(name="list", description="List challenges")
@app_commands.describe(tier="Optional tier filter", year="Year (default 2026)")
@app_commands.choices(tier=TIER_CHOICES)
async def challenge_list(
    interaction: discord.Interaction,
    tier: Optional[app_commands.Choice[str]] = None,
    year: int = 2026,
) -> None:
    items = list_challenges(year=year, tier=tier.value if tier else None)
    if not items:
        await interaction.response.send_message("No challenges found.", ephemeral=True)
        return
    lines = []
    for c in items[:25]:
        param = f" `<{c['param_label']}>`" if c.get("param_label") else ""
        lines.append(
            f"**{c['tier']} #{c['number']}** {c['title']}{param} — {c['description']} "
            f"_(confirm: {verify_label(c)})_"
        )
    embed = discord.Embed(
        title=f"Challenges {year}" + (f" · {tier.name}" if tier else ""),
        description="\n".join(lines)[:4000],
        color=0x5865F2,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@challenge_group.command(name="start", description="Start a challenge (rolling timer)")
@app_commands.describe(
    tier="Difficulty tier",
    number="Challenge number within tier",
    param="Required when the challenge needs a series/game/book/etc.",
    year="Catalog year",
)
@app_commands.choices(tier=TIER_CHOICES)
async def challenge_start(
    interaction: discord.Interaction,
    tier: app_commands.Choice[str],
    number: app_commands.Range[int, 1, 20],
    param: Optional[str] = None,
    year: int = 2026,
) -> None:
    await interaction.response.defer(ephemeral=True)
    result = att_svc.start_attempt(
        str(interaction.user.id),
        tier=tier.value,
        number=int(number),
        param=param,
        guild_id=str(interaction.guild_id) if interaction.guild_id else None,
        display_name=interaction.user.display_name,
        year=year,
    )
    if not result.get("ok"):
        await interaction.followup.send(result.get("error") or "Failed.", ephemeral=True)
        return
    ch = result["challenge"]
    ends = _ts(result["ends_at"])
    param_s = f"\nFocus: **{result['param']}**" if result.get("param") else ""
    howto = confirm_howto(ch)
    await interaction.followup.send(
        f"Started **{ch['tier']} #{ch['number']}: {ch['title']}**{param_s}\n"
        f"{ch.get('description') or ''}\n"
        f"Deadline: <t:{ends}:F> (<t:{ends}:R>)\n\n"
        f"{howto}",
        ephemeral=True,
    )


@challenge_group.command(name="status", description="Your active and recent attempts")
async def challenge_status(interaction: discord.Interaction) -> None:
    active = att_svc.active_attempt(str(interaction.user.id))
    recent = att_svc.list_attempts(discord_id=str(interaction.user.id), limit=8)
    lines = []
    if active:
        ends = _ts(active["ends_at"])
        lines.append(
            f"**Active:** {active['tier']} #{active['number']} {active['title']} "
            f"— ends <t:{ends}:R>"
            + (f" · {active['param']}" if active.get("param") else "")
            + f" · confirm: **{verify_label(active)}**"
        )
        lines.append(confirm_howto(active))
    else:
        lines.append("**Active:** none")
    for a in recent:
        if active and a["id"] == active["id"]:
            continue
        lines.append(
            f"`{a['status']}` {a['tier']} #{a['number']} {a['title']} (#{a['id']})"
        )
    user = att_svc.get_user(str(interaction.user.id))
    tadoku = (
        f"Tadoku: `{user['tadoku_user_id']}`"
        if user and user.get("tadoku_user_id")
        else "Tadoku: **not linked** — `/challenge link`"
    )
    lines.append(tadoku)
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@challenge_group.command(
    name="confirm",
    description="Verify completion via Tadoku (or honor if challenge allows)",
)
@app_commands.describe(note="Optional note (required spirit for honor challenges)")
async def challenge_confirm(
    interaction: discord.Interaction,
    note: Optional[str] = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    result = await att_svc.confirm_attempt(str(interaction.user.id), note=note)
    if not result.get("ok"):
        msg = result.get("error") or "Not completed."
        if result.get("progress"):
            msg += f"\nProgress: {result['progress']}"
        if result.get("log_count") is not None:
            msg += f"\nLogs seen in window: {result['log_count']}"
        await interaction.followup.send(msg, ephemeral=True)
        return

    att = result["attempt"]
    method = result.get("method") or "tadoku"
    # rewards + announce
    if interaction.guild and isinstance(interaction.user, discord.Member):
        roles = await apply_rewards(interaction.user, tier=att["tier"])
        await announce_completion(
            interaction.client,
            guild=interaction.guild,
            user=interaction.user,
            title=att["title"],
            tier=att["tier"],
            number=att["number"],
            param=att.get("param"),
            method=method,
        )
    else:
        roles = []
    extra = f"\nRoles: {', '.join(roles)}" if roles else ""
    detail = ""
    if result.get("verify"):
        detail = f"\n{result['verify'].get('detail')}"
    await interaction.followup.send(
        f"✅ Completed **{att['tier']} #{att['number']}: {att['title']}** ({method}){detail}{extra}",
        ephemeral=True,
    )


@challenge_group.command(name="abandon", description="Abandon your active challenge")
async def challenge_abandon(interaction: discord.Interaction) -> None:
    result = att_svc.abandon_attempt(str(interaction.user.id))
    if not result.get("ok"):
        await interaction.response.send_message(result.get("error") or "Nope", ephemeral=True)
        return
    await interaction.response.send_message("Abandoned.", ephemeral=True)


@challenge_group.command(
    name="link",
    description="Link your Tadoku user id (from profile URL or UUID)",
)
@app_commands.describe(
    user_id="Tadoku user UUID, or profile URL containing it",
    display_name="Optional Tadoku display name",
)
async def challenge_link(
    interaction: discord.Interaction,
    user_id: str,
    display_name: Optional[str] = None,
) -> None:
    raw = user_id.strip()
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
    uid = m.group(0)
    att_svc.link_tadoku(str(interaction.user.id), uid, display_name)
    await interaction.response.send_message(
        f"Linked Tadoku user `{uid}`.", ephemeral=True
    )


@challenge_group.command(name="leaderboard", description="Top completers")
async def challenge_leaderboard(interaction: discord.Interaction) -> None:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT a.discord_id, u.display_name, COUNT(*) AS n "
            "FROM attempts a LEFT JOIN users u ON u.discord_id=a.discord_id "
            "WHERE a.status='completed' GROUP BY a.discord_id "
            "ORDER BY n DESC LIMIT 15"
        ).fetchall()
    if not rows:
        await interaction.response.send_message("No completions yet.", ephemeral=True)
        return
    lines = [
        f"**{i+1}.** {r['display_name'] or r['discord_id']} — **{r['n']}**"
        for i, r in enumerate(rows)
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=False)


@challenge_group.command(name="admin_void", description="Admin: void an attempt by id")
@app_commands.describe(attempt_id="attempts.id")
async def challenge_admin_void(
    interaction: discord.Interaction, attempt_id: int
) -> None:
    if not is_bot_admin(interaction):
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return
    r = att_svc.admin_update_attempt(attempt_id, status="void", verify_method="mod")
    await interaction.response.send_message(
        "Voided." if r.get("ok") else r.get("error"), ephemeral=True
    )


@challenge_group.command(
    name="admin_force_complete",
    description="Admin: force-complete active attempt for a user (manual)",
)
@app_commands.describe(user="Target member", note="Reason")
async def challenge_admin_force(
    interaction: discord.Interaction,
    user: discord.Member,
    note: str = "admin force",
) -> None:
    if not is_bot_admin(interaction):
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    result = await att_svc.confirm_attempt(
        str(user.id), note=note, force_honor=True
    )
    if not result.get("ok"):
        await interaction.followup.send(result.get("error") or "Failed", ephemeral=True)
        return
    att = result["attempt"]
    if interaction.guild:
        await apply_rewards(user, tier=att["tier"])
        await announce_completion(
            interaction.client,
            guild=interaction.guild,
            user=user,
            title=att["title"],
            tier=att["tier"],
            number=att["number"],
            param=att.get("param"),
            method="manual",
        )
    await interaction.followup.send(
        f"Force-completed attempt for {user.mention}.", ephemeral=True
    )


def build_bot() -> ChallengeBot:
    return ChallengeBot()
