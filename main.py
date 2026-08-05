"""
Entry point. Single-process for now — the plan (§9) flags splitting the
Discord client from a FastAPI service as optional at this scale, and
"optional" means "skip it until something forces your hand."
"""
import asyncio
import logging
import sys

from config import config
from db import db
from bot.discord_bot import bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("servebot.main")


async def main():
    problems = config.validate()
    if problems:
        for p in problems:
            log.error(p)
        sys.exit(1)

    log.info("Connecting to Postgres...")
    await db.init_pool(config.database_url)

    try:
        log.info("Starting Discord bot...")
        await bot.start(config.discord_token)
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
