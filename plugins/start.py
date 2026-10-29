import asyncio
import html
import logging

from pyrogram import Client, filters, enums
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo,
)

from config import (
    WELCOME_VIDEO, START_BUTTONS, WELCOME_BUTTONS, GROUP_WELCOME_TXT,
    WELCOME_DELETE_AFTER, PREMIUM_PHOTO, OWNER_LINK,
)
from database.premium_db import get_premium
from database.settings_db import get_settings
from log_utils import register_user
from plugins.deliver import deliver_file
from plugins.verify import handle_notcopy
from utils import IST, temp
from strings import (
    START_TXT, PREMIUM_TEXT, HELP_TXT,
    MYPLAN_ACTIVE_TXT, MYPLAN_NONE_TXT,
    PREMIUM_OWNER_BTN, PREMIUM_MYPLAN_BTN, PREMIUM_BACK_BTN,
    MYPLAN_ALERT_ACTIVE, MYPLAN_ALERT_NONE,
)

logger = logging.getLogger(__name__)
HTML = enums.ParseMode.HTML

_GROUP_TYPES = (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers — button layouts from config.py and "send a playable video" fallbacks
# ══════════════════════════════════════════════════════════════════════════════

def build_markup(layout):
    """config tuples -> InlineKeyboardMarkup.
    Each button: (text, "url" | "callback", value). Bad / empty entries are
    skipped (and logged) so one typo in config.py can never break /start."""
    rows = []
    for row in layout or []:
        buttons = []
        for item in row:
            try:
                text, kind, value = item
                value = str(value).replace("{bot}", temp.U_NAME or "")
                if not value:
                    continue
                if str(kind).lower() == "url":
                    buttons.append(InlineKeyboardButton(text, url=value))
                elif str(kind).lower() in ("callback", "cb", "callback_data"):
                    buttons.append(InlineKeyboardButton(text, callback_data=value))
                else:
                    logger.warning("Unknown button type %r in config.py: %r", kind, item)
            except Exception:
                logger.warning("Bad button entry in config.py: %r", item, exc_info=True)
        if buttons:
            rows.append(buttons)
    return InlineKeyboardMarkup(rows) if rows else None


async def _reply_media(message, text, markup, *, photo=None, video=None, quote=False):
    """Reply with a real photo / playable video + caption. Falls back to plain
    text if the media can't be sent (wrong file_id, bad URL, ...)."""
    kw = dict(caption=text, reply_markup=markup, parse_mode=HTML, quote=quote)
    if photo:
        try:
            return await message.reply_photo(photo, **kw)
        except Exception:
            logger.warning("Couldn't send photo %r — falling back", photo, exc_info=True)
    if video:
        try:
            if str(video).startswith("http"):
                return await message.reply_video(video, supports_streaming=True, **kw)
            return await message.reply_cached_media(video, **kw)
        except Exception:
            logger.warning("Couldn't send welcome video %r — falling back to text", video, exc_info=True)
    return await message.reply_text(
        text, reply_markup=markup, quote=quote, parse_mode=HTML, disable_web_page_preview=True,
    )


async def _swap_message(bot, query, text, markup, *, photo=None, video=None):
    """Turn the message a button was pressed on into another page (video <-> photo
    <-> text). Telegram can't turn text into media (or media into text) in place,
    so in that case the old message is deleted and a fresh one is sent."""
    msg = query.message
    media = None
    if photo:
        media = InputMediaPhoto(photo, caption=text, parse_mode=HTML)
    elif video:
        media = InputMediaVideo(video, caption=text, parse_mode=HTML, supports_streaming=True)

    try:
        if media:
            await msg.edit_media(media, reply_markup=markup)
            return
        if msg.text:
            await msg.edit_text(text, reply_markup=markup, parse_mode=HTML, disable_web_page_preview=True)
            return
    except MessageNotModified:
        return
    except Exception:
        logger.info("In-place edit failed — sending a fresh message", exc_info=True)

    try:
        await msg.delete()
    except Exception:
        pass
    await _reply_media(msg, text, markup, photo=photo, video=video, quote=False)


def _start_text(user) -> str:
    return START_TXT.format(mention=user.mention if user else "there")


async def send_start(message):
    """Welcome video + text + buttons. Same message for /start in DM and in groups;
    groups get link-only buttons (callbacks would let anyone edit a group message)."""
    private = message.chat.type == enums.ChatType.PRIVATE
    layout = START_BUTTONS if private else WELCOME_BUTTONS
    await _reply_media(
        message, _start_text(message.from_user), build_markup(layout),
        video=WELCOME_VIDEO, quote=False,
    )


# ══════════════════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.command("start"))
async def start_cmd(bot, message):
    if message.chat.type != enums.ChatType.PRIVATE:
        # In a group: the welcome video + text (file / verify deep links only run in DM).
        await send_start(message)
        return

    await register_user(message.from_user)

    args = message.text.split(maxsplit=1)
    payload = args[1] if len(args) > 1 else ""

    if payload.startswith("file_"):
        await deliver_file(bot, message.from_user.id, payload[len("file_"):])
        return

    if payload.startswith("notcopy_"):
        await handle_notcopy(bot, message, payload[len("notcopy_"):])
        return

    if payload.startswith("sgfile-"):
        # Get Files button from the support group: exactly what typing these words in the
        # bot DM does (title menu when several titles match, files, relaxed season search...).
        from plugins.search import handle_search

        search_query = payload[len("sgfile-"):].replace("-", " ").strip()
        if search_query:
            message.text = search_query
            await handle_search(bot, message, from_deep_link=True)
        else:
            await send_start(message)
        return

    if payload.startswith("getfile-"):
        # Deep link from a /m movie-update post: run the normal search flow
        # (poster, pagination, season/language/quality filters).
        import html
        from database.filters_db import search_files
        from plugins.search import _deliver_results

        search_query = payload[len("getfile-"):].replace("-", " ").strip()
        results = await search_files(search_query) if search_query else []
        if results:
            await _deliver_results(message, search_query, results)
        else:
            await message.reply_text(f"❌ No results found for <b>{html.escape(search_query)}</b>.")
        return

    await send_start(message)


@Client.on_message(filters.command("help"))
async def help_cmd(_, message):
    await message.reply_text(HELP_TXT)


@Client.on_message(filters.command("myplan"))
async def myplan_cmd(_, message):
    if not message.from_user:  # anonymous group admin
        return
    doc = await get_premium(message.from_user.id)
    if doc:
        expiry = doc["expiry_time"].astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
        await message.reply_text(MYPLAN_ACTIVE_TXT.format(expiry=expiry))
    else:
        await message.reply_text(MYPLAN_NONE_TXT)


# ══════════════════════════════════════════════════════════════════════════════
# Welcome new members — playable video + mention (groups where the bot is admin)
# ══════════════════════════════════════════════════════════════════════════════

_LAST_WELCOME: dict = {}        # chat_id -> message id of the previous welcome
_tasks: set = set()             # keep references so background deletes aren't GC'd


async def _delete_later(message, seconds: int):
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except Exception:
        pass


@Client.on_message(filters.new_chat_members & filters.group)
async def welcome_new_members(bot, message):
    try:
        settings = await get_settings()
        if not settings.get("welcome_enabled", True):
            return

        members = [u for u in (message.new_chat_members or []) if not u.is_bot]
        if not members:        # the bot itself (or other bots) joined
            return

        mention = ", ".join(u.mention for u in members[:5])
        if len(members) > 5:
            mention += f" +{len(members) - 5}"
        text = (
            GROUP_WELCOME_TXT
            .replace("{mention}", mention)
            .replace("{chat}", html.escape(message.chat.title or "the group"))
        )

        sent = await _reply_media(
            message, text, build_markup(WELCOME_BUTTONS), video=WELCOME_VIDEO, quote=False,
        )

        # keep the chat clean: drop the previous welcome, and expire this one
        prev = _LAST_WELCOME.get(message.chat.id)
        if prev:
            try:
                await bot.delete_messages(message.chat.id, prev)
            except Exception:
                pass
        _LAST_WELCOME[message.chat.id] = sent.id
        if WELCOME_DELETE_AFTER > 0:
            task = asyncio.create_task(_delete_later(sent, WELCOME_DELETE_AFTER))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
    except Exception:
        logger.warning("welcome_new_members failed", exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
# Premium page (callback "premium") — photo + PREMIUM_TEXT (strings.py) + Owner / Back
# ══════════════════════════════════════════════════════════════════════════════

def _premium_markup() -> InlineKeyboardMarkup:
    top = [InlineKeyboardButton(PREMIUM_MYPLAN_BTN, callback_data="premium_myplan")]
    if OWNER_LINK:
        top.insert(0, InlineKeyboardButton(PREMIUM_OWNER_BTN, url=OWNER_LINK))
    return InlineKeyboardMarkup([top, [InlineKeyboardButton(PREMIUM_BACK_BTN, callback_data="start_back")]])


@Client.on_callback_query(filters.regex(r"^premium$"))
async def premium_page(bot, query):
    await query.answer()
    await _swap_message(bot, query, PREMIUM_TEXT, _premium_markup(), photo=PREMIUM_PHOTO or None)


@Client.on_callback_query(filters.regex(r"^start_back$"))
async def back_to_start(bot, query):
    await query.answer()
    await _swap_message(
        bot, query, _start_text(query.from_user), build_markup(START_BUTTONS),
        video=WELCOME_VIDEO or None,
    )


@Client.on_callback_query(filters.regex(r"^premium_myplan$"))
async def premium_myplan(_, query):
    doc = await get_premium(query.from_user.id)
    if doc:
        expiry = doc["expiry_time"].astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
        await query.answer(MYPLAN_ALERT_ACTIVE.format(expiry=expiry), show_alert=True)
    else:
        await query.answer(MYPLAN_ALERT_NONE, show_alert=True)
