from __future__ import annotations

import asyncio
import logging
import os

import discord
import uvicorn

from app.bot import build_bot
from app.catalog import upsert_challenges_from_yaml
from app.config import get_settings
from app.db import init_db
from app.services.attempts import expire_overdue
from app.web.app import create_app

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("challengetracker")

_BACKOFF_MIN = 5.0
_BACKOFF_MAX = 120.0
_TOKEN_SLEEP = 300.0


async def _close_bot(bot: discord.Client | None) -> None:
    if bot is None:
        return
    try:
        if not bot.is_closed():
            await bot.close()
    except Exception:  # noqa: BLE001
        logger.warning("error closing Discord client", exc_info=True)


async def run() -> None:
    settings = get_settings()
    init_db(settings.database_path)
    try:
        n = upsert_challenges_from_yaml(settings.challenge_catalog, replace_year=False, seed_only=True)
        logger.info(
            "Catalog seed/upsert: %s challenges from %s", n, settings.challenge_catalog
        )
    except FileNotFoundError:
        logger.warning("Catalog file missing: %s", settings.challenge_catalog)
    expired = expire_overdue()
    if expired:
        logger.info("Startup expired %s attempt(s)", len(expired))

    # Bot is attached later — web reads app.state.bot dynamically.
    app = create_app(bot=None)
    config = uvicorn.Config(
        app,
        host=settings.ui_host,
        port=settings.ui_port,
        log_level=settings.log_level.lower(),
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    async def run_web() -> None:
        await server.serve()

    async def run_bot() -> None:
        """Keep the admin UI up if the gateway dies. discord.py close() is one-shot."""
        delay = _BACKOFF_MIN
        token = settings.discord_token
        while True:
            await _close_bot(getattr(app.state, "bot", None))
            b = build_bot()
            app.state.bot = b
            became_ready = asyncio.Event()

            async def _mark_ready() -> None:
                await b.wait_until_ready()
                became_ready.set()

            marker = asyncio.create_task(_mark_ready())
            try:
                async with b:
                    await b.start(token)
                logger.warning("Discord bot.start returned; retry in %.0fs", delay)
            except asyncio.CancelledError:
                raise
            except discord.LoginFailure:
                logger.exception(
                    "Discord login failed (check DISCORD_TOKEN); retry in %.0fs",
                    _TOKEN_SLEEP,
                )
                await asyncio.sleep(_TOKEN_SLEEP)
                delay = _BACKOFF_MIN
                continue
            except Exception:
                logger.exception("Discord bot error; retry in %.0fs", delay)
            finally:
                marker.cancel()
                try:
                    await marker
                except asyncio.CancelledError:
                    pass
            if became_ready.is_set():
                delay = _BACKOFF_MIN
            await asyncio.sleep(delay)
            if not became_ready.is_set():
                delay = min(delay * 2.0, _BACKOFF_MAX)

    async def run_backups() -> None:
        from app.services.db_backup import run_due_backups

        # First pass soon after boot, then hourly.
        delay = 15.0
        while True:
            await asyncio.sleep(delay)
            delay = 3600.0
            try:
                result = await asyncio.to_thread(run_due_backups)
                for w in result.get("warnings") or []:
                    logger.warning("backup: %s", w)
                if result.get("done"):
                    logger.info("backup: %s", ", ".join(result["done"]))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("backup loop error")

    web_task = asyncio.create_task(run_web(), name="admin-web")
    backup_task = asyncio.create_task(run_backups(), name="db-backup")
    bot_task: asyncio.Task | None = None
    if settings.discord_token:
        bot_task = asyncio.create_task(run_bot(), name="discord-bot")
        logger.info("Discord bot starting")
    else:
        logger.warning(
            "DISCORD_TOKEN empty — admin UI only. Set token in .env and rebuild/restart."
        )

    try:
        await web_task
    finally:
        for t in (backup_task, bot_task):
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        await _close_bot(getattr(app.state, "bot", None))


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
