from __future__ import annotations

import logging
from datetime import timezone

import discord
from discord.ext import commands, tasks

from app.bot_commands import challenge_group
from app.config import get_settings, merge_runtime
from app.db import db_session, get_all_settings
from app.services import attempts as att_svc
from app.services import progress_status as prog_svc
from app.services.rewards import announce
from app.services.verify import parse_iso

logger = logging.getLogger(__name__)


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
    """Guild owner, Administrator permission, or ADMIN_ROLE_IDS."""
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
        activated, expired = att_svc.tick_lifecycle()
        if activated:
            logger.info("Activated %s scheduled attempt(s)", len(activated))
        if expired:
            logger.info("Expired %s attempt(s)", len(expired))

        async def _post(kind: str, att: dict, **extra: object) -> None:
            user = None
            try:
                uid = int(att["discord_id"])
                user = self.get_user(uid) or await self.fetch_user(uid)
            except (TypeError, ValueError, discord.HTTPException):
                user = None
            guild = None
            gid = att.get("guild_id") or _runtime().get("guild_id")
            if gid and str(gid).isdigit():
                guild = self.get_guild(int(gid))
            kwargs = dict(
                kind=kind,
                guild=guild,
                user=user,
                display_name=att.get("display_name") or att.get("discord_id"),
                title=att.get("title") or "?",
                tier=att.get("tier") or "?",
                number=int(att.get("number") or 0),
                param=att.get("param"),
                content=att_svc.display_content(att),
                media_filter=att.get("media_filter"),
            )
            kwargs.update(extra)
            await announce(self, **kwargs)  # type: ignore[arg-type]

        # Scheduled → live: post a fresh START card (schedule card already went out earlier)
        for att in activated:
            ends_ts = None
            try:
                ends_ts = _ts(att["ends_at"])
            except Exception:  # noqa: BLE001
                pass
            await _post("start", att, ends_at_ts=ends_ts)
        # Window closed without a clear → FAILED/EXPIRED card (no congrats)
        for att in expired:
            ends_ts = None
            try:
                ends_ts = _ts(att.get("ends_at"))
            except Exception:  # noqa: BLE001
                pass
            await _post("expired", att, ends_at_ts=ends_ts)

        # Automatic mid-run status (time milestones all; goal milestones Tadoku-only)
        if get_settings().status_updates_enabled:
            try:
                n = await self._run_status_updates()
                if n:
                    logger.info("Posted %s active status update(s)", n)
            except Exception as exc:  # noqa: BLE001
                logger.warning("status updates failed: %s", exc)

    async def _run_status_updates(self) -> int:
        """Time + Tadoku goal milestone cards for active attempts."""
        posted = 0
        actives = prog_svc.list_active_attempts()
        for att in actives:
            try:
                events, meta = await prog_svc.collect_status_events_for_attempt(att)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "status collect failed attempt=%s: %s", att.get("id"), exc
                )
                continue
            if not events:
                continue
            for ev in events:
                ends_ts = None
                try:
                    ends_ts = _ts(att["ends_at"])
                except Exception:  # noqa: BLE001
                    pass
                user = None
                try:
                    uid = int(att["discord_id"])
                    user = self.get_user(uid) or await self.fetch_user(uid)
                except (TypeError, ValueError, discord.HTTPException):
                    user = None
                guild = None
                gid = att.get("guild_id") or _runtime().get("guild_id")
                if gid and str(gid).isdigit():
                    guild = self.get_guild(int(gid))
                try:
                    await announce(
                        self,
                        kind=ev.kind,
                        guild=guild,
                        user=user,
                        display_name=att.get("display_name") or att.get("discord_id"),
                        title=att.get("title") or "?",
                        tier=att.get("tier") or "?",
                        number=int(att.get("number") or 0),
                        param=att.get("param"),
                        content=att_svc.display_content(att),
                        line_primary=ev.primary,
                        line_secondary=ev.secondary,
                        ends_at_ts=ends_ts,
                        media_filter=att.get("media_filter"),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("status announce failed: %s", exc)
                    continue
                meta = prog_svc.apply_event_marks(meta, ev)
                prog_svc.save_status_meta(int(att["id"]), meta)
                posted += 1
        return posted

    @expire_loop.before_loop
    async def before_expire(self) -> None:
        await self.wait_until_ready()


def build_bot() -> ChallengeBot:
    return ChallengeBot()
