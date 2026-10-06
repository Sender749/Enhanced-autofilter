"""
Admin Commands Plugin

Provides a unified /admin command that shows all available admin commands
with descriptions in a formatted menu.
"""
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMINS
from database.filters_db import total_files
from database.settings_db import get_settings
from strings import SETTINGS_MAIN_TXT

ADMIN_PANEL_TXT = """<b>🔧 Admin Commands Panel</b>

<b>📋 Available Commands:</b>
<b>• /aliases</b> - List search aliases

<b>📚 Indexing & Stats:</b>
<b>• /index</b> - Index an entire channel (auto + manual)
<b>• /stats</b> - Full bot dashboard (channels, users, groups, DB, server)
<b>• /delete</b> <code>file_link</code> - Delete one file (database or database + channel)
<b>• /deleteall</b> - Delete all indexed files from MongoDB <i>(PM)</i>

<b>🖼 Media Links:</b>
<b>• /telegraph</b> - Reply to a photo/video (or send one) to get a link + file_id <i>(admin only)</i>

<b>🎬 Movie Updates:</b>
<b>• /m title [year] [s02]</b> - Post a movie/series update (e.g. <code>/m pushpa 2</code>, <code>/m suits s02</code>)

<b>💎 Premium Management:</b>
<b>• /add_premium</b> - Add premium user
<b>• /remove_premium</b> - Remove premium user

<b>👤 User Management:</b>
<b>• /id</b> - User info (reply, or <code>/id user_id</code>)
<b>• /send</b> <code>id [id…]</code> - Reply to a message to send it to users
<b>• /ban</b> <code>user_id</code> - Ban a user (asks for a reason)
<b>• /unban</b> <code>user_id</code> - Unban a user
<b>• /showban</b> - List banned users with reasons

<b>📊 File Limit:</b>
<b>• /checklimit</b> <code>user_id</code> - Check a user's free file usage
<b>• /resetlimit</b> <code>user_id</code> - Reset one user's limit
<b>• /resetlimitall</b> - Reset everyone's limit <i>(PM)</i>

<b>👥 Groups & Broadcast:</b>
<b>• /show_groups</b> - Groups where bot is admin <i>(PM)</i>
<b>• /leave_groups</b> <code>group_id</code> - Make the bot leave a group
<b>• /broadcast</b> - Reply to a message to send it to all users <i>(PM)</i>
<b>• /syncusers</b> - Import old users into the broadcast list <i>(PM)</i>
<b>• /extra</b> - Short list of the extra commands
<b>• /link</b> <code>name [year] [s01 e05]</code> - Build a bot search link
<b>• Admin call</b> - users mentioning @admin / the bot are forwarded to admins

<b>🔗 Verification Setup:</b>
<b>• /set_shortener</b> - Set verification shortener
<b>• /set_verify_time</b> - Set verification time gaps
<b>• /set_tutorial</b> - Set verification tutorial links

<b>🚫 Search Filters:</b>
<b>• /set_filterword</b> <code>word, phrase</code> - Words ignored in every search
<b>• /remove_filterword</b> <code>word, phrase</code> - Remove ignored words
<b>• /filterwords</b> - Show all filter words
<b>• /trending</b> - Most searched titles

<b>ℹ️ Note:</b> Auto movie updates, fetch channels and requests are managed through the settings panel."""


@Client.on_message(filters.command("admin") & filters.user(ADMINS))
async def admin_commands_panel(_, message):
    """Show admin commands panel with all available commands."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Settings Panel", callback_data="admin_settings")],
        [InlineKeyboardButton("📊 View Stats", callback_data="admin_stats")],
    ])
    await message.reply_text(
        ADMIN_PANEL_TXT,
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


@Client.on_callback_query(filters.regex(r"^admin_") & filters.user(ADMINS))
async def admin_callbacks(bot, query):
    """Handle admin panel callbacks."""
    action = query.data.split("_", 1)[1]

    if action == "settings":
        # Imported lazily: settings.py is a sibling plugin.
        from plugins.settings import build_main_menu
        settings = await get_settings()
        await query.answer()
        await query.message.edit_text(SETTINGS_MAIN_TXT, reply_markup=build_main_menu(settings))
        return

    if action == "stats":
        count = await total_files()
        await query.answer(f"Indexed files: {count}", show_alert=True)
        return

    await query.answer()
