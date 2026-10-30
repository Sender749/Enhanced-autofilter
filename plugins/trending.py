"""
/trending — most searched titles as coloured buttons, 10 per page.

Works in groups and in the bot's DM. Only titles that were found in the
database are listed, always spelled as in the database (see
database/trending_db.py). Tapping a title runs the normal search flow
(poster, pagination, season/language/quality filters).
"""
import asyncio
import math

from pyrogram import Client, filters
from pyrogram.errors import RPCError
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database.filters_db import clean_display_text
from database.settings_db import get_settings
from database.trending_db import get_page, get_by_key
from strings import (
    MAINTENANCE_TXT, TRENDING_HEADER_TXT, TRENDING_EMPTY_TXT,
    TRENDING_NOT_IN_DB_TXT, SEARCH_EXPIRED_TXT,
)

TRENDING_PER_PAGE = 10


async def _build(page: int):
    """-> (text, markup | None) for one page (page is clamped into range)."""
    docs, total = await get_page(max(0, page), TRENDING_PER_PAGE)
    if total == 0:
        return TRENDING_EMPTY_TXT, None

    pages = max(1, math.ceil(total / TRENDING_PER_PAGE))
    if page >= pages:  # list shrank / stale button — jump to the last page
        page = pages - 1
        docs, total = await get_page(page, TRENDING_PER_PAGE)

    rows = []
    for doc in docs:
        name = clean_display_text(doc["name"]) or doc["name"]
        label = "🔥 " + (name if len(name) <= 55 else name[:52] + "…")
        rows.append([InlineKeyboardButton(label, callback_data=f"trq#{doc['_id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"trp#{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"trp#{page + 1}"))
    rows.append(nav)

    return TRENDING_HEADER_TXT, InlineKeyboardMarkup(rows)


@Client.on_message(filters.command("trend"))
async def trending_cmd(_, message):
    settings = await get_settings()
    if not settings["autofilter_enabled"]:
        await message.reply_text(MAINTENANCE_TXT, quote=True)
        return
    text, markup = await _build(0)
    await message.reply_text(text, reply_markup=markup, quote=True, disable_web_page_preview=True)


@Client.on_callback_query(filters.regex(r"^trp#\d+$"))
async def trending_page(_, query):
    await query.answer()
    text, markup = await _build(int(query.data.split("#")[1]))
    try:
        await query.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except RPCError:  # e.g. MESSAGE_NOT_MODIFIED on a double tap
        pass


@Client.on_callback_query(filters.regex(r"^trq#"))
async def trending_clicked(_, query):
    # Imported here to avoid a circular import (search.py is a sibling plugin).
    from database.filters_db import search_files
    from plugins.search import _deliver_results

    settings = await get_settings()
    if not settings["autofilter_enabled"]:
        await query.answer("🛠 Bot is under maintenance. Please try again later.", show_alert=True)
        return

    doc = await get_by_key(query.data.split("#", 1)[1])
    if not doc:
        await query.answer(SEARCH_EXPIRED_TXT, show_alert=True)
        return

    name = doc["name"]
    shown = clean_display_text(name) or name  # no emojis/symbols on screen
    results = await search_files(name)
    if not results:
        # File was deleted since it was counted — tell the user, keep the list as is.
        await query.answer(TRENDING_NOT_IN_DB_TXT.format(title=shown[:100]), show_alert=True)
        return

    await query.answer()
    await _deliver_results(query.message, shown, results)
