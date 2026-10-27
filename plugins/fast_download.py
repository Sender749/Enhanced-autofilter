"""
"⚡ Fast Download" button handler. The button sits under every delivered file
(added in deliver._send_file, only when the feature is configured). A tap shows
the validity popup INSTANTLY (after a quick quota check) and builds the link in
the background, so the user never waits on Telegram or the database:
  1. checks the user's daily link quota,
  2. makes sure the file has a live copy in BIN_CHANNEL (copied once, reused;
     re-copied automatically if the copy was deleted or BIN_CHANNEL changed),
  3. picks a host (Oracle first, Koyeb/Render as fallback),
  4. swaps the button for Download (+ Watch for videos) links,
  5. logs who generated the link in BIN_CHANNEL, as a reply to the file.
Delivery itself and all its gates (premium / force-sub / limit / verify) are
untouched — this only runs after the file has already been delivered.
"""
import asyncio
import html
import logging

from pyrogram import Client, enums, filters
from pyrogram.errors import RPCError

from config import (
    BIN_CHANNEL, FASTDL_ENABLED, STREAM_LINK_TTL_HOURS,
    STREAM_DAILY_LIMIT, STREAM_PREMIUM_DAILY_LIMIT,
)
from database.filters_db import get_file_by_id, display_name
from database.premium_db import has_premium_access
from database.stream_db import (
    get_bin_entry, save_bin_entry, get_daily, increment_daily,
)
from fastdl.hosts import choose_host, note_link_issued
from fastdl.links import build_url, is_video, ready_markup
from strings import (
    FILE_NOT_FOUND_TXT, FAST_GENERATING_TXT, FAST_LIMIT_REACHED_TXT,
    FAST_UNAVAILABLE_TXT, FAST_ERROR_TXT, BIN_USER_INFO_TXT,
)

logger = logging.getLogger(__name__)
_busy: set = set()  # (user, file) pairs mid-request, so double-taps can't double-count
_tasks: set = set()  # keep references to fire-and-forget tasks


async def _answer(query, text: str, alert: bool = False):
    try:
        await query.answer(text, show_alert=alert)
    except RPCError:
        pass  # query too old — nothing useful to do


async def _copy_is_alive(bot, entry: dict) -> bool:
    """The cached BIN copy is only usable if it's in the CURRENT BIN_CHANNEL
    and the message still exists with media."""
    if entry.get("bin_chat") != BIN_CHANNEL:
        return False
    try:
        msg = await bot.get_messages(BIN_CHANNEL, entry["bin_msg_id"])
    except RPCError:
        return False
    return bool(msg and not msg.empty and (msg.document or msg.video or msg.audio))


async def _copy_to_bin(bot, file_id: str, doc: dict, replace: bool) -> dict:
    sent = await bot.send_cached_media(
        chat_id=BIN_CHANNEL, file_id=doc["file_id"], caption=display_name(doc)[:900],
    )
    media = sent.document or sent.video or sent.audio
    size = int(doc.get("file_size") or getattr(media, "file_size", 0) or 0)
    return await save_bin_entry(
        file_id, sent.id, BIN_CHANNEL, size, doc.get("file_name") or "file", replace=replace,
    )


async def _log_user_in_bin(bot, entry: dict, doc: dict, user):
    """Reply to the file's BIN copy with who generated a link (name is a
    clickable mention). Never allowed to break link generation."""
    try:
        full_name = " ".join(p for p in (user.first_name, user.last_name) if p) or "User"
        user_link = f'<a href="tg://user?id={user.id}">{html.escape(full_name)}</a>'
        if user.username:
            user_link += f" (@{html.escape(user.username)})"
        text = BIN_USER_INFO_TXT.format(
            file_name=html.escape(doc.get("file_name") or display_name(doc)),
            user_id=user.id,
            user_link=user_link,
        )
        try:
            await bot.send_message(
                BIN_CHANNEL, text,
                reply_to_message_id=entry["bin_msg_id"],
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except RPCError:
            # The file copy may have been deleted from the channel — still keep the log line.
            await bot.send_message(
                BIN_CHANNEL, text,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except Exception:
        logger.warning("Couldn't post user info to BIN_CHANNEL", exc_info=True)


async def _fail(query, text: str):
    """The popup was already used up, so problems after it are shown as a short
    reply under the file (auto-deleted). The Fast Download button stays, so the
    user can simply tap it again."""
    try:
        note = await query.message.reply_text(text, quote=True)
        await asyncio.sleep(10)
        await note.delete()
    except Exception:
        pass


async def _generate(bot, query, file_id: str, user, key):
    try:
        doc = await get_file_by_id(file_id)
        if not doc:
            await _fail(query, FILE_NOT_FOUND_TXT)
            return

        entry = await get_bin_entry(file_id)
        if not entry or not await _copy_is_alive(bot, entry):
            entry = await _copy_to_bin(bot, file_id, doc, replace=bool(entry))

        size = int(entry.get("size") or 0)
        host = await choose_host(size)
        if not host:
            logger.error("Fast download: no stream host available for file %s (user %s) — see the lines above",
                         file_id, user.id)
            await _fail(query, FAST_UNAVAILABLE_TXT)
            return

        name = entry.get("name") or "file"
        url = build_url(host.base_url, entry["bin_msg_id"], user.id, name)
        watch_url = None
        if is_video(name, doc.get("mime_type", "")):
            # Same token, same host: one link counts once against quota and bandwidth.
            token = url.split("/dl/", 1)[1].split("/", 1)[0]
            watch_url = f"{host.base_url}/watch/{token}/{url.rsplit('/', 1)[1]}"

        await query.message.edit_reply_markup(ready_markup(url, watch_url))
        await note_link_issued(host, size)
        await increment_daily(user.id)
        await _log_user_in_bin(bot, entry, doc, user)
    except Exception:
        logger.exception("Fast download failed for file %s (user %s)", file_id, user.id)
        await _fail(query, FAST_ERROR_TXT)
    finally:
        _busy.discard(key)


@Client.on_callback_query(filters.regex(r"^fdl#"))
async def fast_download(bot, query):
    if not FASTDL_ENABLED:
        await _answer(query, "Fast download isn't available.", True)
        return

    file_id = query.data.split("#", 1)[1]
    user = query.from_user
    key = (user.id, file_id)
    if key in _busy:
        await _answer(query, "⏳ Working on it...")
        return

    limit = STREAM_PREMIUM_DAILY_LIMIT if await has_premium_access(user.id) else STREAM_DAILY_LIMIT
    if await get_daily(user.id) >= limit:
        await _answer(query, FAST_LIMIT_REACHED_TXT.format(limit=limit), True)
        return

    # Popup first — the user reads it while the link is built in the background.
    _busy.add(key)
    await _answer(query, FAST_GENERATING_TXT.format(hours=STREAM_LINK_TTL_HOURS), True)
    task = asyncio.create_task(_generate(bot, query, file_id, user, key))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
