from __future__ import annotations

import asyncio
import logging
import os
import sys

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


async def run() -> None:
    settings = get_settings()
    init_db(settings.database_path)
    try:
        n = upsert_challenges_from_yaml(settings.challenge_catalog, replace_year=False)
        logger.info("Catalog seed/upsert: %s challenges from %s", n, settings.challenge_catalog)
    except FileNotFoundError:
        logger.warning("Catalog file missing: %s", settings.challenge_catalog)
    expired = expire_overdue()
    if expired:
        logger.info("Startup expired %s attempt(s)", len(expired))

    bot = None
    if settings.discord_token:
        bot = build_bot()

    app = create_app(bot=bot)
    config = uvicorn.Config(
        app,
        host=settings.ui_host,
        port=settings.ui_port,
        log_level=settings.log_level.lower(),
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    web_task = asyncio.create_task(server.serve(), name="admin-web")

    bot_task = None
    if bot is not None:

        async def run_bot() -> None:
            async with bot:
                await bot.start(settings.discord_token)

        bot_task = asyncio.create_task(run_bot(), name="discord-bot")
        logger.info("Discord bot starting")
    else:
        logger.warning(
            "DISCORD_TOKEN empty — admin UI only. Set token in .env and rebuild/restart."
        )

    tasks = {web_task}
    if bot_task:
        tasks.add(bot_task)

    done, pending = await asyncio.wait(
        tasks,
        return_when=asyncio.FIRST_EXCEPTION,
    )
    for t in done:
        if t.exception():
            logger.error("Task failed: %s", t.exception())
    for t in pending:
        t.cancel()
    server.should_exit = True
    if bot is not None and not bot.is_closed:
        await bot.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
