import asyncio
import logging
import time

from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMINS, LOG_CHANNEL
from database.filters_db import save_file
from database.settings_db import get_settings, add_index_channel
from utils import temp, readable_time, Throttle

logger = logging.getLogger(__name__)

_index_lock = asyncio.Lock()

_MEDIA_TYPES = (enums.MessageMediaType.VIDEO, enums.MessageMediaType.DOCUMENT)


def _extract_media(message):
    """Return the Document/Video object for a message, with .caption and
    .file_type attached, or None if this message has nothing indexable."""
    if not message.media or message.media not in _MEDIA_TYPES:
        return None
    media = getattr(message, message.media.value, None)
    if not media:
        return None
    media.caption = message.caption.html if message.caption else ""
    media.file_type = message.media.value
    return media


# ══════════════════════════════════════════════════════════════════════════════
# AUTO-INDEX — fires the moment a file is posted to a source channel.
# The channel list is admin-editable at runtime (see /settings -> Index), so
# membership is checked against the cached settings on every message rather
# than a filter list frozen at plugin-load time. Since settings are cached
# in memory, this costs nothing extra per message.
# ══════════════════════════════════════════════════════════════════════════════

async def _is_index_channel(_, __, message) -> bool:
    settings = await get_settings()
    return message.chat.id in settings["index_channels"]


index_channel_filter = filters.create(_is_index_channel)


@Client.on_message(index_channel_filter & (filters.document | filters.video))
async def auto_index(_, message):
    media = _extract_media(message)
    if not media:
        return
    status = await save_file(media, channel_id=message.chat.id)
    if status == "error":
        logger.warning("Auto-index failed for message %s in %s", message.id, message.chat.id)


@Client.on_edited_message(index_channel_filter & (filters.document | filters.video))
async def auto_reindex_edited(_, message):
    """An admin edited the caption of an indexed post: refresh the stored caption right away
    (save_file only writes when something actually changed)."""
    media = _extract_media(message)
    if media:
        await save_file(media, channel_id=message.chat.id)


# ══════════════════════════════════════════════════════════════════════════════
# MANUAL INDEX — admin walks the bot through backfilling a whole channel
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.command("index") & filters.private & filters.user(ADMINS))
async def request_index(bot, message):
    if _index_lock.locked():
        await message.reply_text("⏳ An index job is already running. Wait for it to finish.")
        return

    prompt = await message.reply_text(
        "Forward the <b>last message</b> to index from the channel, or send its link."
    )
    try:
        reply = await bot.listen(chat_id=message.chat.id, user_id=message.from_user.id, timeout=120)
    except asyncio.TimeoutError:
        await prompt.edit_text("⌛ Timed out waiting for a message.")
        return
    await prompt.delete()

    last_msg_id = chat_id = None
    if reply.text and reply.text.startswith("https://t.me"):
        parts = reply.text.strip().split("/")
        try:
            last_msg_id = int(parts[-1])
            raw_chat = parts[-2]
            chat_id = int("-100" + raw_chat) if raw_chat.isnumeric() else raw_chat
        except (ValueError, IndexError):
            await message.reply_text("That doesn't look like a valid message link.")
            return
    elif reply.forward_from_chat and reply.forward_from_chat.type == enums.ChatType.CHANNEL:
        last_msg_id = reply.forward_from_message_id
        chat_id = reply.forward_from_chat.id
    else:
        await message.reply_text("Send a forwarded channel message, or a t.me link — nothing else.")
        return

    try:
        chat = await bot.get_chat(chat_id)
    except Exception as exc:
        await message.reply_text(f"Couldn't open that channel: <code>{exc}</code>")
        return
    if chat.type != enums.ChatType.CHANNEL:
        await message.reply_text("I can only index channels.")
        return

    skip_prompt = await message.reply_text("How many messages should I skip from the start? Send <code>0</code> for none.")
    try:
        skip_reply = await bot.listen(chat_id=message.chat.id, user_id=message.from_user.id, timeout=60)
        skip = int(skip_reply.text.strip())
    except (asyncio.TimeoutError, ValueError, AttributeError):
        await skip_prompt.edit_text("Invalid or missing number — cancelled.")
        return
    await skip_prompt.delete()

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Start indexing", callback_data=f"idx#go#{chat_id}#{last_msg_id}#{skip}")],
        [InlineKeyboardButton("✖️ Cancel", callback_data="idx#cancel_dialog")],
    ])
    await message.reply_text(
        f"Index <b>{chat.title}</b>?\nMessages to scan: <code>{last_msg_id - skip}</code>",
        reply_markup=buttons,
    )


@Client.on_callback_query(filters.regex(r"^idx#"))
async def index_callback(bot, query):
    parts = query.data.split("#")
    action = parts[1]

    if action == "cancel_dialog":
        await query.message.edit_text("Cancelled.")
        return

    if action == "stop":
        temp.CANCEL_INDEX = True
        await query.answer("Stopping after the current batch…")
        return

    if action == "go":
        if _index_lock.locked():
            await query.answer("Another index job is already running.", show_alert=True)
            return
        _, _, chat_id, last_msg_id, skip = parts
        chat_id = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        await query.message.edit_text("🚀 Indexing started…")
        asyncio.create_task(_run_index(bot, query.message, chat_id, int(last_msg_id), int(skip)))


async def _run_index(bot, status_msg, chat_id, last_msg_id, skip):
    start = time.time()
    saved = updated = duplicate = skipped = errors = 0
    current = skip
    throttle = Throttle(interval=3.0)

    async with _index_lock:
        temp.CANCEL_INDEX = False
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Stop", callback_data="idx#stop")]])

        async for message in _iter_messages(bot, chat_id, last_msg_id, skip):
            current += 1
            if temp.CANCEL_INDEX:
                break

            media = _extract_media(message) if message else None
            if media is None:
                skipped += 1
            else:
                status = await save_file(media, channel_id=chat_id)
                if status == "saved":
                    saved += 1
                elif status == "updated":
                    updated += 1
                elif status == "duplicate":
                    duplicate += 1
                else:
                    skipped += 1 if status == "skipped" else 0
                    errors += 1 if status == "error" else 0

            if throttle.ready():
                text = (
                    f"📥 Indexing…\n"
                    f"Scanned: <code>{current - skip}</code> / <code>{last_msg_id - skip}</code>\n"
                    f"Saved: <code>{saved}</code>  Updated: <code>{updated}</code>\n"
                    f"Duplicates: <code>{duplicate}</code>  Skipped: <code>{skipped}</code>  Errors: <code>{errors}</code>"
                )
                try:
                    await status_msg.edit_text(text, reply_markup=cancel_btn)
                except (MessageNotModified, Exception):
                    pass

        # A manual backfill means the admin wants this channel tracked —
        # register it for auto-indexing too, so future posts aren't missed.
        await add_index_channel(chat_id)

        elapsed = readable_time(time.time() - start)
        final_text = (
            f"{'⏹ Stopped' if temp.CANCEL_INDEX else '✅ Done'} in {elapsed}\n"
            f"Saved: <code>{saved}</code>  Updated: <code>{updated}</code>\n"
            f"Duplicates: <code>{duplicate}</code>  Skipped: <code>{skipped}</code>  Errors: <code>{errors}</code>\n"
            f"📌 This channel is now set up for auto-indexing too."
        )
        try:
            await status_msg.edit_text(final_text)
        except MessageNotModified:
            pass
        if LOG_CHANNEL:
            try:
                await bot.send_message(LOG_CHANNEL, f"Index job on {chat_id}: {final_text}")
            except Exception:
                pass


async def _iter_messages(bot, chat_id, last_msg_id, skip, batch_size=200):
    """Walk a channel's message-id range in batches, tolerating FloodWait."""
    current = skip
    while current < last_msg_id:
        ids = list(range(current + 1, min(current + batch_size, last_msg_id) + 1))
        try:
            messages = await bot.get_messages(chat_id, ids)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            continue
        for message in messages:
            yield message
        current += batch_size
