"""
Every route that ends with "send this file to the user" — the /start
deep-link, the fsub "Try Again" button, the post-verification button — goes
through `deliver_file()`. One place enforces premium bypass, force-sub,
the daily file limit, and verification, so those features can never drift
out of sync with each other or accidentally be skipped by a new entry
point later.

Gate order (mirrors the reference bot exactly):
  1. Premium         -> unlimited, skips everything below.
  2. Force-sub        -> must join first.
  3. Daily file limit  -> only evaluated when verification is ALSO on,
                          since verification is the only way out of it;
                          under the limit, sends immediately with no
                          verification step at all.
  4. Verification       -> only reached once the free quota (if any) is
                            used up, or immediately if there's no quota.
"""
import asyncio
import logging

from pyrogram import Client, filters
from pyrogram.errors import RPCError
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database.filters_db import get_file_by_id, display_name
from database.premium_db import has_premium_access
from database.settings_db import get_settings
from database.verify_db import required_tier, create_token
from database.limit_db import get_today_count, increment_today
from plugins.force_sub import missing_channels
from fastdl.links import start_button_markup
from shortlink import make_short_link
from utils import temp, format_duration
from strings import (
    FILE_NOT_FOUND_TXT, FILE_SEND_CAPTION, FILE_SEND_CAPTION_WITH_LIMIT,
    FSUB_REQUIRED_TXT, TRY_AGAIN_BTN,
    VERIFY_PROMPT_TXT, VERIFY_BTN, VERIFY_TUTORIAL_BTN,
    FILE_AUTODELETE_NOTICE, FILE_AUTODELETE_DONE,
)

logger = logging.getLogger(__name__)


async def deliver_file(bot, user_id: int, file_id: str):
    """Run every gate and, if they all pass, send the file to `user_id`'s PM."""
    doc = await get_file_by_id(file_id)
    if not doc:
        await bot.send_message(user_id, FILE_NOT_FOUND_TXT)
        return

    if await has_premium_access(user_id):
        await _send_file(bot, user_id, doc)
        return

    missing = await missing_channels(bot, user_id)
    if missing:
        try:
            mention = (await bot.get_users(user_id)).mention
        except RPCError:
            mention = f"user {user_id}"
        missing.append([InlineKeyboardButton(TRY_AGAIN_BTN, callback_data=f"retry#{file_id}")])
        await bot.send_message(
            user_id,
            FSUB_REQUIRED_TXT.format(mention=mention),
            reply_markup=InlineKeyboardMarkup(missing),
        )
        return

    settings = await get_settings()

    # File limit only has teeth when verification is on — it's the only way
    # to get more files once the quota runs out, so without it the "limit"
    # would just be a dead end. Skip the whole thing in that case.
    if settings["file_limit_enabled"] and settings["verify_enabled"]:
        used = await get_today_count(user_id)
        limit = settings["file_limit_count"]
        if used < limit:
            await increment_today(user_id)
            await _send_file(bot, user_id, doc, remaining=(used + 1, limit))
            return
        # Quota used up for today — fall through to verification below.

    if settings["verify_enabled"]:
        tier = await required_tier(user_id, settings["verify_time"], settings["third_verify_time"])
        if tier:
            await _send_verify_prompt(bot, user_id, tier, file_id, settings)
            return

    await _send_file(bot, user_id, doc)


async def _send_file(bot, user_id: int, doc: dict, remaining: tuple | None = None):
    name = display_name(doc)
    if remaining:
        used, limit = remaining
        caption = FILE_SEND_CAPTION_WITH_LIMIT.format(file_name=name, used=used, limit=limit)
    else:
        caption = FILE_SEND_CAPTION.format(file_name=name)

    try:
        sent = await bot.send_cached_media(
            chat_id=user_id, file_id=doc["file_id"], caption=caption,
            reply_markup=start_button_markup(str(doc["_id"])),
        )
    except RPCError:
        logger.exception("Failed to deliver file %s to %s", doc.get("_id"), user_id)
        await bot.send_message(user_id, FILE_NOT_FOUND_TXT)
        return

    settings = await get_settings()
    if settings["file_autodelete_enabled"]:
        asyncio.create_task(_schedule_file_delete(bot, sent, settings["file_autodelete_seconds"]))


async def _schedule_file_delete(bot, sent_message, seconds: int):
    """Runs in the background — never blocks the delivery response."""
    notice = None
    try:
        notice = await bot.send_message(
            sent_message.chat.id,
            FILE_AUTODELETE_NOTICE.format(duration=format_duration(seconds)),
            reply_to_message_id=sent_message.id,
        )
    except RPCError:
        pass

    await asyncio.sleep(seconds)

    try:
        await sent_message.delete()
    except RPCError:
        pass
    if notice:
        try:
            await notice.edit_text(FILE_AUTODELETE_DONE)
        except RPCError:
            pass


async def _send_verify_prompt(bot, user_id: int, tier: int, file_id: str, settings: dict):
    token = await create_token(user_id, tier, file_id)
    shortener = settings["shorteners"].get(str(tier), {})
    long_url = f"https://t.me/{temp.U_NAME}?start=notcopy_{token}"
    short_url = await make_short_link(long_url, shortener.get("domain", ""), shortener.get("api", ""))

    buttons = [[InlineKeyboardButton(VERIFY_BTN, url=short_url)]]
    tutorial = settings["tutorials"].get(str(tier), "")
    if tutorial:
        buttons.append([InlineKeyboardButton(VERIFY_TUTORIAL_BTN, url=tutorial)])

    try:
        mention = (await bot.get_users(user_id)).mention
    except RPCError:
        mention = f"user {user_id}"

    await bot.send_message(
        user_id,
        VERIFY_PROMPT_TXT.format(mention=mention, tier=tier),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_callback_query(filters.regex(r"^retry#"))
async def retry_delivery(bot, query):
    file_id = query.data.split("#", 1)[1]
    await query.answer()
    try:
        await query.message.delete()
    except RPCError:
        pass
    await deliver_file(bot, query.from_user.id, file_id)
