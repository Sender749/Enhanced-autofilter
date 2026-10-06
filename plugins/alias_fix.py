"""
Turn #FILE_NOT_FOUND reports into search aliases.

Flow (all inside the private not-found channel):
  1. Each report has  [✏️ Fix] [🚫 Ignore]  (added in plugins/search.py).
  2. Fix    -> message says "reply to this message with the correct name".
  3. An admin replies in the channel with the correct title.
  4. The title is searched in the database:
       match     -> alias saved, report edited to "✅ query -> Title".
       no match  -> bot asks:  [Save anyway] [Try another name] [Ignore]
  5. Once an alias exists, that spelling finds the movie for every user
     (search_files() applies it to the title part of the query).

Notes
  * The channel is private. A reply posted in a channel comes from the channel
    itself, so the replying admin can't be identified; what protects it is that
    only the channel's admins can post there. Button presses DO identify the
    person, so they're checked against ADMINS as well.
  * First admin to press a button wins (atomic state change in Mongo).
  * /aliases lists aliases, /delalias <query> removes one.
"""
import html
import logging

from pyrogram import Client, filters, enums
from pyrogram.errors import RPCError
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMINS, NOT_FOUND_FILE_CHANNEL
from database.alias_db import (
    alias_key, save_alias, delete_alias, list_aliases, transition,
    find_waiting_by_reply, other_open_with_key, misses,
)
from database.filters_db import (
    search_files, clean_title, display_name, parse_query, parse_tag_only_query,
)

logger = logging.getLogger(__name__)

HTML = enums.ParseMode.HTML
ADMIN = filters.user(ADMINS)


def _is_nf_channel(_, __, message) -> bool:
    return bool(NOT_FOUND_FILE_CHANNEL) and message.chat is not None and message.chat.id == NOT_FOUND_FILE_CHANNEL


nf_channel_filter = filters.create(_is_nf_channel)


def _open_markup(miss_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Fix", callback_data=f"nfa#fix#{miss_id}"),
        InlineKeyboardButton("🚫 Ignore", callback_data=f"nfa#ign#{miss_id}"),
    ]])


def _waiting_markup(miss_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"nfa#can#{miss_id}")]])


def _confirm_markup(miss_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Save anyway", callback_data=f"nfa#sav#{miss_id}")],
        [InlineKeyboardButton("✏️ Try another name", callback_data=f"nfa#try#{miss_id}"),
         InlineKeyboardButton("🚫 Ignore", callback_data=f"nfa#ign#{miss_id}")],
    ])


async def _edit(bot, chat_id: int, msg_id: int, text: str, markup=None):
    try:
        await bot.edit_message_text(chat_id, msg_id, text, parse_mode=HTML, reply_markup=markup)
    except RPCError as exc:
        # "message is not modified" etc. are harmless
        logger.debug("Edit of %s failed: %s", msg_id, exc)


async def _delete_quietly(bot, chat_id: int, *msg_ids):
    ids = [m for m in msg_ids if m]
    if not ids:
        return
    try:
        await bot.delete_messages(chat_id, ids)
    except RPCError:
        pass


def _done_text(doc: dict, target_title: str, note: str = "") -> str:
    return (
        f"{doc['text']}\n\n✅ <b>Alias saved:</b> <code>{html.escape(doc['key'])}</code> → "
        f"<b>{html.escape(target_title)}</b>{note}"
    )


async def _finish_alias(bot, doc: dict, target_key: str, target_title: str, note: str = ""):
    """Save the alias, edit the report, and tidy up other open reports with
    the same normalised query (they're solved by the same alias)."""
    await save_alias(doc["key"], target_key, target_title)
    chat_id = NOT_FOUND_FILE_CHANNEL
    await _edit(bot, chat_id, doc["chat_msg_id"], _done_text(doc, target_title, note))
    await _delete_quietly(bot, chat_id, doc.get("followup_msg_id"))

    for other in await other_open_with_key(doc["key"], doc["_id"]):
        updated = await transition(str(other["_id"]), ["open"], "done")
        if updated and updated.get("chat_msg_id"):
            await _edit(bot, chat_id, updated["chat_msg_id"], _done_text(updated, target_title, " (same query)"))


# ── buttons ─────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^nfa#(fix|ign|can|sav|try)#"))
async def nf_buttons(bot, query):
    if query.from_user.id not in ADMINS:
        await query.answer("Admins only.", show_alert=True)
        return

    _, action, miss_id = query.data.split("#", 2)
    chat_id = NOT_FOUND_FILE_CHANNEL
    handled = "Already handled by another admin."

    if action == "fix":
        doc = await transition(miss_id, ["open"], "waiting")
        if not doc:
            return await query.answer(handled, show_alert=True)
        await _edit(bot, chat_id, doc["chat_msg_id"],
                    f"{doc['text']}\n\n↩️ <b>Reply to this message with the correct name.</b>",
                    _waiting_markup(miss_id))
        return await query.answer("Reply to the message with the correct name.")

    if action == "can":
        doc = await transition(miss_id, ["waiting", "confirm"], "open")
        if not doc:
            return await query.answer(handled, show_alert=True)
        await _edit(bot, chat_id, doc["chat_msg_id"], doc["text"], _open_markup(miss_id))
        await _delete_quietly(bot, chat_id, doc.get("followup_msg_id"))
        return await query.answer("Cancelled.")

    if action == "ign":
        doc = await transition(miss_id, ["open", "waiting", "confirm"], "ignored")
        if not doc:
            return await query.answer(handled, show_alert=True)
        await _edit(bot, chat_id, doc["chat_msg_id"], f"{doc['text']}\n\n🚫 <b>Ignored.</b>")
        await _delete_quietly(bot, chat_id, doc.get("followup_msg_id"))
        return await query.answer("Ignored.")

    if action == "try":
        doc = await transition(miss_id, ["confirm"], "waiting")
        if not doc:
            return await query.answer(handled, show_alert=True)
        await _edit(bot, chat_id, doc["chat_msg_id"],
                    f"{doc['text']}\n\n↩️ <b>Reply to this message with the correct name.</b>",
                    _waiting_markup(miss_id))
        await _delete_quietly(bot, chat_id, doc.get("followup_msg_id"))
        return await query.answer("Reply with another name.")

    if action == "sav":
        doc = await transition(miss_id, ["confirm"], "done")
        if not doc or not doc.get("pending_key"):
            return await query.answer(handled, show_alert=True)
        await _finish_alias(bot, doc, doc["pending_key"], doc["pending_title"],
                            "\n⚠️ No files with this title yet.")
        return await query.answer("Alias saved.")


# ── the admin's reply in the channel ────────────────────────────────────────

@Client.on_message(nf_channel_filter & filters.reply & filters.text, group=-2)
async def nf_reply(bot, message):
    doc = await find_waiting_by_reply(message.reply_to_message_id)
    if not doc:
        return  # an ordinary message in the channel — not ours
    try:
        await _process_reply(bot, message, doc)
    finally:
        message.stop_propagation()  # we own this reply; nothing else should react to it


async def _process_reply(bot, message, doc: dict):
    typed = (message.text or "").strip()
    if not typed or typed.startswith("/"):
        return

    chat_id = NOT_FOUND_FILE_CHANNEL
    title_part = parse_query(typed)[0]
    target_key = clean_title(title_part).lower()

    async def warn(text: str):
        return await bot.send_message(chat_id, text, parse_mode=HTML, reply_to_message_id=message.id)

    if not target_key or parse_tag_only_query(typed) is not None:
        await warn("⚠️ That has no title in it (only language/year/quality). Reply with the movie name.")
        return
    if target_key == doc["key"]:
        await warn("⚠️ That is the same as the user's query. Reply with the <b>correct</b> name.")
        return

    # Look in the database WITHOUT aliases, so this is a real title match.
    results = await search_files(typed, use_alias=False)
    if results:
        target_title = clean_title(display_name(results[0])) or target_key
        updated = await transition(str(doc["_id"]), ["waiting", "confirm"], "done")
        if not updated:
            return
        await _finish_alias(bot, updated, target_key, target_title,
                            f"\n📁 {len(results)} file(s) found.")
        await _delete_quietly(bot, chat_id, message.id)
        return

    # No match: ask the admin what to do.
    shown = clean_title(title_part) or typed
    updated = await transition(str(doc["_id"]), ["waiting", "confirm"], "confirm",
                               {"pending_title": shown, "pending_key": target_key})
    if not updated:
        return
    await _delete_quietly(bot, chat_id, updated.get("followup_msg_id"))
    sent = await bot.send_message(
        chat_id,
        f"⚠️ <b>{html.escape(shown)}</b> has no files in the database.\n"
        f"Alias <code>{html.escape(doc['key'])}</code> → <b>{html.escape(shown)}</b> would find nothing yet.",
        parse_mode=HTML,
        reply_to_message_id=doc["chat_msg_id"],
        reply_markup=_confirm_markup(str(doc["_id"])),
    )
    await misses.update_one({"_id": doc["_id"]}, {"$set": {"followup_msg_id": sent.id}})
    await _delete_quietly(bot, chat_id, message.id)


# ── /aliases and /delalias ──────────────────────────────────────────────────

@Client.on_message(filters.command("aliases") & ADMIN)
async def aliases_cmd(bot, message):
    docs, total = await list_aliases(40)
    if not docs:
        return await message.reply_text("No aliases yet.")
    lines = [f"{i}. <code>{html.escape(d['_id'])}</code> → {html.escape(d.get('target_title') or d['target_key'])}"
             for i, d in enumerate(docs, 1)]
    head = f"<b>Search aliases</b> ({total} total, newest {len(docs)} shown)\n\n"
    tail = "\n\nRemove one: <code>/delalias wrong spelling</code>"
    await message.reply_text(head + "\n".join(lines) + tail, parse_mode=HTML)


@Client.on_message(filters.command("delalias") & ADMIN)
async def delalias_cmd(bot, message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: <code>/delalias wrong spelling</code>", parse_mode=HTML)
    text = message.text.split(None, 1)[1]
    key = alias_key(text)
    if key and await delete_alias(key):
        await message.reply_text(f"Alias <code>{html.escape(key)}</code> removed.", parse_mode=HTML)
    else:
        await message.reply_text("No alias found for that. Check /aliases.")
