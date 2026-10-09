"""/app opens the web app; /webapp_sync and /webapp_status are admin-only maintenance commands."""
import asyncio

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from config import ADMINS, WEBAPP_ENABLED, WEBAPP_URL


@Client.on_message(filters.command("app") & filters.private)
async def app_cmd(_, message):
    if not (WEBAPP_ENABLED and WEBAPP_URL):
        await message.reply_text("The web app isn't available right now.")
        return
    await message.reply_text(
        "🎬 Browse movies, series and anime with posters — latest on top.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open MoviesHub", web_app=WebAppInfo(url=f"{WEBAPP_URL}/app"))]]),
    )


@Client.on_message(filters.command("webapp_status") & filters.user(ADMINS))
async def status_cmd(_, message):
    from webapp.catalog import items, titles
    n_items, n_titles = await items.estimated_document_count(), await titles.estimated_document_count()
    pending = await titles.count_documents({"meta_status": "pending"})
    nometa = await titles.count_documents({"meta_status": "none"})
    await message.reply_text(
        f"🎬 <b>Web app catalog</b>\nFiles parsed: <code>{n_items}</code>\nTitles: <code>{n_titles}</code>\n"
        f"Waiting for poster lookup: <code>{pending}</code>\nNo poster found (fallback card): <code>{nometa}</code>"
    )


@Client.on_message(filters.command("webapp_sync") & filters.user(ADMINS))
async def sync_cmd(_, message):
    """/webapp_sync = pick up new files now; /webapp_sync full = re-parse every file AND re-check every poster match."""
    from webapp.catalog import sync
    full = "full" in message.text.lower()
    note = await message.reply_text("⏳ Re-parsing every file…" if full else "⏳ Syncing new files…")

    async def run():
        n = await sync(full=full, rematch=full)
        await note.edit_text(f"✅ Catalog sync done: <code>{n}</code> files processed.")
    asyncio.create_task(run())
