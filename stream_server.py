"""
Standalone stream server — run this on the Oracle Always-Free VM:

    python3 stream_server.py

It does NOT run the bot (no plugins, no database, no updates). It only serves
/dl/ links using the helper bot tokens, so it can't clash with the main bot.
Required env: API_ID, API_HASH, BIN_CHANNEL, STREAM_SECRET (same as the bot),
HELPER_BOT_TOKENS, PORT. See FASTDL_SETUP.md.
"""
import os

os.environ.setdefault("STREAM_ONLY", "true")  # before config is imported

import asyncio
import logging

from aiohttp import web

from config import API_ID, API_HASH, BOT_TOKEN, BIN_CHANNEL, STREAM_SECRET, HELPER_BOT_TOKENS, PORT
from fastdl.pool import pool
from fastdl.server import register_routes
from fastdl.usage import counter

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger("stream_server")


async def main():
    if not (BIN_CHANNEL and STREAM_SECRET):
        raise SystemExit("Set BIN_CHANNEL and STREAM_SECRET (same values as the bot).")
    tokens = list(HELPER_BOT_TOKENS) + ([BOT_TOKEN] if BOT_TOKEN else [])
    if not tokens:
        raise SystemExit("Set HELPER_BOT_TOKENS (and make each helper bot an admin of BIN_CHANNEL).")

    await pool.start(API_ID, API_HASH, tokens, BIN_CHANNEL)
    if not pool.workers:
        raise SystemExit("No stream client could start — check the bot tokens.")

    await counter.start()  # real bytes sent -> STREAM_USAGE_FILE, reported to the bot via /health
    app = web.Application()
    app.add_routes([web.get("/", lambda _: web.Response(text="ok"))])
    register_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("Stream server up on :%s with %s client(s)", PORT, len(pool.workers))
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
