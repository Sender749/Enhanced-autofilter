"""
/exportqueries — export every search users sent (and what the bot did with it)
as a text file, like "Export All Captions". Admins only, in the bot's DM.

    /exportqueries              everything stored
    /exportqueries 7            last 7 days
    /exportqueries missed       only searches that were NOT found
    /exportqueries 30 missed    both

The same export is the "📄 Export Queries" button in /settings.
"""
import logging
import os
import time

from pyrogram import Client, filters

from config import ADMINS
from database.query_log_db import export_queries

logger = logging.getLogger(__name__)


async def send_query_export(message, days: int | None = None, missed_only: bool = False):
    path = f"/tmp/queries_export_{int(time.time())}.txt"
    try:
        unique, total = await export_queries(path, days, missed_only)
        if total == 0:
            await message.reply_text("📭 No searches saved yet (or query saving is switched off in /settings).")
            return
        scope = f"last {days} days" if days else "all stored"
        extra = " · only NOT found" if missed_only else ""
        await message.reply_document(
            path, caption=f"📄 <b>User search queries</b>\n{unique} unique queries · {total} searches · {scope}{extra}",
        )
    except Exception:
        logger.exception("Query export failed")
        await message.reply_text("⚠️ Couldn't create the export. Check the logs.")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@Client.on_message(filters.command("exportqueries") & filters.private & filters.user(ADMINS))
async def export_queries_cmd(bot, message):
    args = [a.lower() for a in message.command[1:]]
    days = next((int(a) for a in args if a.isdigit() and int(a) > 0), None)
    await send_query_export(message, days, "missed" in args)
