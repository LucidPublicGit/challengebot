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
from app.services.monthly_digest import run_monthly_digest
from app.services.rewards import announce, apply_rewards, send_user_only
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


def _manage_howto(attempt_id: int | None = None) -> str:
    """Commands to check/update an open attempt (not confirm-specific)."""
    abandon = (
        f"`/challenge abandon attempt_id:{attempt_id}`"
        if attempt_id is not None
        else "`/challenge abandon`"
    )
    return "\n".join(
        [
            "**Update / check anytime**",
            "`/challenge status` — progress + this guide again",
            "`/challenge content` — labels / focus param",
            "`/challenge logs` + `/challenge match` — fix Tadoku title matching",
            f"{abandon} — cancel this run",
        ]
    )


def confirm_howto(ch: dict, *, attempt_id: int | None = None) -> str:
    """Plain-English how this challenge is confirmed (shown on start/list/status)."""
    mode = (ch.get("verify") or "tadoku").lower()
    kind = (ch.get("goal_kind") or "").lower()
    amount = ch.get("goal_amount")
    media = ch.get("media_filter") or "any"
    param = ch.get("param_label")

    if mode == "honor":
        lines = [
            "**How to complete (honor)**",
            "Tadoku is **not** checked for this challenge.",
            "When you finish, run `/challenge confirm`.",
        ]
        if kind == "immersion_hours":
            lines.append(
                f"Goal: **{amount:g} hours** of immersion (any media you count). "
                "Track time yourself — the bot trusts your confirm."
            )
        elif kind == "complete_work":
            lines.append(
                "Goal: finish the full work end-to-end. "
                "You’ll name it once on confirm if you didn’t set a focus at start."
            )
        else:
            lines.append("Complete the goal, then confirm yourself.")
    else:
        goal_bits = {
            "characters": f"≥ **{amount:,.0f}** characters on Tadoku",
            "minutes": f"≥ **{amount:g}** minutes on Tadoku",
            "volumes": f"≥ **{amount:g}** manga volumes (from Tadoku logs / page estimates)",
            "episodes": f"≥ **{amount:g}** show episodes (from Tadoku logs)",
            "immersion_hours": f"≥ **{amount:g}** hours from timed Tadoku logs",
        }.get(kind, f"meet goal `{kind}` = {amount} via Tadoku")

        lines = [
            "**How to complete (Tadoku-verified)**",
            "`/challenge confirm` checks your Tadoku logs in this window immediately.",
            f"Goal check: {goal_bits} (media filter: `{media}`).",
            "1. Link once: `/challenge link` with your Tadoku user id/URL",
            "2. Log immersion on tadoku.app **during** the timer",
            "3. `/challenge confirm` — no form; bot pulls Tadoku and clears if the goal is met",
            "If confirm **fails**: `/challenge failoverride` (honor, no admin) "
            "or fix titles via `/challenge logs` + `/challenge match`.",
        ]
        if param:
            lines.append(
                f"Start focus **{param}** filters which Tadoku titles count "
                "(`/challenge content param:` / `clear_param` to change)."
            )

    lines.append("")
    lines.append(_manage_howto(attempt_id))
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
        self.monthly_digest_loop.start()
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
            try:
                dur_h = float(att.get("time_limit_hours") or 0) or None
            except (TypeError, ValueError):
                dur_h = None
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
                duration_hours=dur_h,
            )
            kwargs.update(extra)
            await announce(self, **kwargs)  # type: ignore[arg-type]

        # Scheduled → live: post a fresh START card (schedule card already went out earlier)
        for att in activated:
            ends_ts = starts_ts = None
            try:
                ends_ts = _ts(att["ends_at"])
            except Exception:  # noqa: BLE001
                pass
            try:
                starts_ts = _ts(att["started_at"])
            except Exception:  # noqa: BLE001
                pass
            await _post("start", att, ends_at_ts=ends_ts, starts_at_ts=starts_ts)
            # Private how-to (ephemeral only exists on slash start; schedule needs this)
            try:
                uid = int(att["discord_id"])
                user = self.get_user(uid) or await self.fetch_user(uid)
            except (TypeError, ValueError, discord.HTTPException):
                user = None
            guild = None
            gid = att.get("guild_id") or _runtime().get("guild_id")
            if gid and str(gid).isdigit():
                guild = self.get_guild(int(gid))
            if user is not None:
                title = att.get("title") or "challenge"
                body = (
                    f"▶ **Your challenge is live** — "
                    f"{att.get('tier')} #{att.get('number')} **{title}**\n"
                    f"Ends <t:{ends_ts or 0}:R>.\n\n"
                    f"{confirm_howto(att, attempt_id=int(att['id']))}"
                )[:1900]
                await send_user_only(
                    self,
                    guild=guild,
                    user=user,
                    content=body,
                    thread_name=f"Live · {title}"[:100],
                )
        # Window closed without a clear → FAILED/EXPIRED card (no congrats)
        for att in expired:
            ends_ts = None
            try:
                ends_ts = _ts(att.get("ends_at"))
            except Exception:  # noqa: BLE001
                pass
            await _post("expired", att, ends_at_ts=ends_ts)

        rt = _runtime()
        # Automatic mid-run status (time milestones all; goal milestones Tadoku-only)
        if rt.get("status_updates_enabled", True):
            try:
                n = await self._run_status_updates()
                if n:
                    logger.info("Posted %s active status update(s)", n)
            except Exception as exc:  # noqa: BLE001
                logger.warning("status updates failed: %s", exc)

        # Private ~1h-left nudge (private thread preferred; DM fallback)
        if rt.get("status_final_hour_dm", True):
            try:
                n = await self._run_final_hour_reminders()
                if n:
                    logger.info("Sent %s final-hour reminder(s)", n)
            except Exception as exc:  # noqa: BLE001
                logger.warning("final-hour reminders failed: %s", exc)

    async def _run_status_updates(self, *, force: bool = False) -> int:
        """Time + Tadoku goal milestone cards; auto-complete when goal_ok."""
        posted = 0
        actives = prog_svc.list_active_attempts()
        for att in actives:
            try:
                events, meta = await prog_svc.collect_status_events_for_attempt(
                    att, force=force
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "status collect failed attempt=%s: %s", att.get("id"), exc
                )
                continue

            aid = int(att["id"])
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

            # Fresh Tadoku poll met the goal → complete (no separate confirm step)
            if (
                _runtime().get("status_auto_complete", True)
                and meta.get("goal_ok")
                and prog_svc.attempt_is_active(aid)
            ):
                try:
                    result = await att_svc.confirm_attempt(
                        str(att["discord_id"]),
                        attempt_id=aid,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "auto-complete failed attempt=%s: %s", aid, exc
                    )
                    result = {}
                if result.get("ok"):
                    done = result.get("attempt") or att
                    method = result.get("method") or "tadoku_poll"
                    content = result.get("content") or att_svc.display_content(done)
                    detail = None
                    v = result.get("verify") or {}
                    if isinstance(v, dict):
                        detail = v.get("detail")
                    if guild is not None and isinstance(user, discord.Member):
                        try:
                            await apply_rewards(user, tier=done.get("tier") or "easy")
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("auto-complete rewards: %s", exc)
                    try:
                        await announce(
                            self,
                            kind="complete",
                            guild=guild,
                            user=user,
                            display_name=done.get("display_name")
                            or att.get("display_name")
                            or att.get("discord_id"),
                            title=done.get("title") or "?",
                            tier=done.get("tier") or "?",
                            number=int(done.get("number") or 0),
                            param=done.get("param"),
                            content=content,
                            method=method,
                            detail=detail,
                            media_filter=done.get("media_filter"),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("auto-complete announce: %s", exc)
                    meta = dict(meta)
                    meta["goal_met_hint"] = True
                    meta["goal_ok"] = False
                    prog_svc.save_status_meta(aid, meta)
                    posted += 1
                    logger.info(
                        "Auto-completed attempt=%s via Tadoku poll (%s)",
                        aid,
                        detail or method,
                    )
                    continue
                logger.info(
                    "goal_ok but confirm declined attempt=%s: %s",
                    aid,
                    result.get("error") or result,
                )

            if not events:
                continue
            for ev in events:
                # Skip nudge card if we already tried complete above
                if ev.mark_goal_met:
                    meta = prog_svc.apply_event_marks(meta, ev)
                    prog_svc.save_status_meta(aid, meta)
                    continue
                ends_ts = None
                try:
                    ends_ts = _ts(att["ends_at"])
                except Exception:  # noqa: BLE001
                    pass
                try:
                    try:
                        dur_h = float(att.get("time_limit_hours") or 0) or None
                    except (TypeError, ValueError):
                        dur_h = None
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
                        duration_hours=dur_h,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("status announce failed: %s", exc)
                    continue
                meta = prog_svc.apply_event_marks(meta, ev)
                prog_svc.save_status_meta(aid, meta)
                posted += 1
        return posted

    async def _run_final_hour_reminders(self) -> int:
        """Once when ≤1h left: private thread (else DM) with complete/update howto."""
        sent = 0
        for att in prog_svc.list_active_attempts():
            aid = int(att["id"])
            meta = prog_svc.get_status_meta(aid)
            if not prog_svc.due_final_hour_dm(att, meta):
                continue
            if not prog_svc.attempt_is_active(aid):
                continue
            try:
                ends_ts = _ts(att["ends_at"])
            except Exception:  # noqa: BLE001
                ends_ts = 0
            focus = att_svc.display_content(att)
            tier = att.get("tier") or "?"
            number = att.get("number") or "?"
            title = att.get("title") or "?"
            lines = [
                f"⏰ **Final hour** — {tier} #{number} **{title}**",
                f"Ends <t:{ends_ts}:R> (<t:{ends_ts}:f>).",
            ]
            desc = (att.get("description") or "").strip()
            if desc:
                lines.append(desc)
            if focus:
                lines.append(f"Focus: **{focus}**")
            lines.append("")
            lines.append(confirm_howto(att, attempt_id=aid))
            body = "\n".join(lines)[:1900]
            try:
                if not prog_svc.attempt_is_active(aid):
                    continue
                uid = int(att["discord_id"])
                user = self.get_user(uid) or await self.fetch_user(uid)
                guild = None
                gid = att.get("guild_id") or _runtime().get("guild_id")
                if gid and str(gid).isdigit():
                    guild = self.get_guild(int(gid))
                how = await send_user_only(
                    self,
                    guild=guild,
                    user=user,
                    content=body,
                    thread_name=f"Final hour · {title}"[:100],
                )
                if how:
                    sent += 1
                    meta = prog_svc.mark_final_hour_dm(meta)
                    prog_svc.save_status_meta(aid, meta)
                else:
                    logger.info(
                        "final-hour notice undeliverable attempt=%s user=%s",
                        aid,
                        att.get("discord_id"),
                    )
                    # Can't deliver → mark so we don't retry every minute
                    meta = prog_svc.mark_final_hour_dm(meta)
                    prog_svc.save_status_meta(aid, meta)
            except (TypeError, ValueError, discord.HTTPException) as exc:
                logger.warning(
                    "final-hour reminder failed attempt=%s: %s", aid, exc
                )
                # leave unmarked → retry next tick
        return sent

    @expire_loop.before_loop
    async def before_expire(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=30)
    async def monthly_digest_loop(self) -> None:
        try:
            kind = await run_monthly_digest(self)
            if kind:
                logger.info("Monthly digest: %s", kind)
        except Exception as exc:  # noqa: BLE001
            logger.warning("monthly digest loop: %s", exc)

    @monthly_digest_loop.before_loop
    async def before_monthly_digest(self) -> None:
        await self.wait_until_ready()


def build_bot() -> ChallengeBot:
    return ChallengeBot()
