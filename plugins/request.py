"""
Request plugin - handles user file requests to admins.
Follows the clean architecture pattern of this repo.
"""
import asyncio
import html
import logging
import re
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import UserIsBlocked, RPCError

from config import ADMINS, REQUEST_CHANNEL, MOVIE_GROUP_LINK
from database.request_db import add_request
from database.settings_db import get_settings
from utils import format_duration
from strings import (
    REQUEST_SENT_TXT, REQUEST_RECEIVED_TXT, REQUEST_NOT_CONFIGURED_TXT, REQUEST_HELP_TXT,
    QUERY_AUTODELETE_NOTE,
    ALREADY_AVAILABLE_TXT, NOT_RELEASED_TXT, CHECK_SPELLING_TXT, UPLOADED_TXT,
    NOT_AVAILABLE_TXT, YEAR_LANGUAGE_TXT, CUSTOM_REPLY_TXT
)

logger = logging.getLogger(__name__)


_bg_tasks: set = set()     # strong refs so background deletes aren't garbage-collected


async def _delete_later(message, seconds: int):
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except Exception:
        pass


def _requested_name(msg) -> str:
    """Pull ONLY the requested file name out of a request-channel message.

    The channel message also carries the user's mention, ID and the admin
    instruction line (REQUEST_RECEIVED_TXT). Those are for admins and must
    never be forwarded to the requester, so we extract just the query.
    Returned HTML-escaped, ready to drop inside <code>...</code>.
    """
    text = (getattr(msg, "text", None) or "").strip()
    m = re.search(r"Query:\s*(.*?)\s*(?:\n\s*\nPlease check|$)", text, re.S)
    name = m.group(1).strip() if m else ""
    return html.escape(name or "your request")


def _status_rows(link) -> list:
    """Button rows under every reply sent to the requester: View Status, plus
    the movie group (MOVIE_GROUP_LINK in config.py) when it is set."""
    rows = [[InlineKeyboardButton("♻️ View Status ♻️", url=link)]]
    if MOVIE_GROUP_LINK:
        rows.append([InlineKeyboardButton("🎬 Movie Group 🎬", url=MOVIE_GROUP_LINK)])
    return rows


# In-memory deduplication to prevent spam: user_id -> (key, sent request message)
_REQUEST_DEDUP = {}
# Track custom reply prompts
_CUSTOM_REPLY_WAIT = {}
# Track wrong spelling prompts
_WRONG_SPELL_WAIT = {}


async def send_request(bot, user_id: int, query: str, username: str = None, origin_message=None):
    """
    Send a file request to the admin channel.
    
    Args:
        bot: The bot instance
        user_id: The user's Telegram ID
        query: The search query/file name being requested
        username: The user's username (optional)
        origin_message: The original message (for group context)
    """
    if not REQUEST_CHANNEL:
        return None
    
    # Deduplication check
    # Same user asking for the same thing again → don't spam the channel, but
    # hand back the original request so the user still gets their confirmation.
    dedup_key = f"{user_id}:{query.lower().strip()}"
    previous = _REQUEST_DEDUP.get(user_id)
    if previous and previous[0] == dedup_key:
        return previous[1]
    
    # Store in database
    username = username or f"ID: {user_id}"
    await add_request(user_id, query, username)
    
    # Build buttons
    buttons = []
    if origin_message and origin_message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        try:
            buttons.append([InlineKeyboardButton("👀 View Request", url=origin_message.link)])
        except Exception:
            pass
    else:
        buttons.append([InlineKeyboardButton("👀 View Request", callback_data=f"view_req_{user_id}")])
    
    # Add show options button for admins
    buttons.append([InlineKeyboardButton(
        "⚙ Show Options",
        callback_data=f"show_options#{user_id}#{origin_message.id if origin_message else 0}"
    )])
    
    # Send to request channel
    user_mention = f"<a href='tg://user?id={user_id}'>{html.escape(str(username))}</a>"
    request_text = REQUEST_RECEIVED_TXT.format(
        user_mention=user_mention,
        user_id=user_id,
        query=html.escape(query)
    )
    
    try:
        sent = await bot.send_message(
            REQUEST_CHANNEL,
            request_text,
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None
        )
        _REQUEST_DEDUP[user_id] = (dedup_key, sent)   # only remembered once it really went out
        return sent
    except Exception:
        logger.exception("Failed to send request to channel %s", REQUEST_CHANNEL)
        return None


@Client.on_callback_query(filters.regex(r"^view_req_"))
async def view_request_callback(bot, query):
    """Handle callback to view user request info."""
    user_id = int(query.data.split("_")[2])
    if query.from_user.id not in ADMINS:
        await query.answer("⚠️ Admin only!", show_alert=True)
        return
    
    await query.answer("Viewing request info...")
    # You can implement additional logic here to show request details


@Client.on_callback_query(filters.regex(r"^show_options"))
async def show_options_callback(bot, query):
    """Show admin response options for a request."""
    _, user_id, msg_id = query.data.split("#")
    chnl_id = query.message.chat.id
    userid = query.from_user.id
    
    buttons = [
        [InlineKeyboardButton("🫤 Already Available", callback_data=f"already_available#{user_id}#{msg_id}"), 
         InlineKeyboardButton("🚫 Not Released Yet", callback_data=f"not_released#{user_id}#{msg_id}")],
        [InlineKeyboardButton("📅 Tell Me Year/Language", callback_data=f"year#{user_id}#{msg_id}"), 
         InlineKeyboardButton("✏️ Check Your Spelling", callback_data=f"upload_in#{user_id}#{msg_id}")],
        [InlineKeyboardButton("✅ Uploaded", callback_data=f"uploaded#{user_id}#{msg_id}"), 
         InlineKeyboardButton("❌ Not Available", callback_data=f"not_available#{user_id}#{msg_id}")],
        [InlineKeyboardButton("📝 Uploaded, Wrong Spelling", callback_data=f"spl_wrong#{user_id}#{msg_id}")],
        [InlineKeyboardButton("💬 Custom Reply", callback_data=f"custom_reply#{user_id}#{msg_id}")]
    ]
    
    try:
        st = await bot.get_chat_member(chnl_id, userid)
        if st.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await query.message.edit_reply_markup(InlineKeyboardMarkup(buttons))
        elif st.status == enums.ChatMemberStatus.MEMBER:
            await query.answer("⚠️ Admin only!", show_alert=True)
    except Exception:
        await query.answer("⚠️ You are not a member of this channel, first join", show_alert=True)


@Client.on_callback_query(filters.regex(r"^not_released"))
async def not_released_callback(bot, query):
    """Handle 'Not Released Yet' response."""
    _, user_id, msg_id = query.data.split("#")
    chnl_id = query.message.chat.id
    
    buttons = [[InlineKeyboardButton("🚫 Not Released 🚫", callback_data=f"na_alert#{user_id}")]]
    btn = [
        *_status_rows(f"{query.message.link}")
    ]
    
    st = await bot.get_chat_member(chnl_id, query.from_user.id)
    if st.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
        user = await bot.get_users(int(user_id))
        request = _requested_name(query.message)
        await query.answer("Message sent to requester")
        await query.message.edit_text(f"<s>{html.escape(query.message.text)}</s>")
        await query.message.edit_reply_markup(InlineKeyboardMarkup(buttons))
        try:
            await bot.send_message(
                chat_id=int(user_id), 
                text=NOT_RELEASED_TXT.format(requested_name=request),
                reply_markup=InlineKeyboardMarkup(btn)
            )
        except UserIsBlocked:
            pass
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


@Client.on_callback_query(filters.regex(r"^not_available"))
async def not_available_callback(bot, query):
    """Handle 'Not Available' response."""
    _, user_id, msg_id = query.data.split("#")
    chnl_id = query.message.chat.id
    
    buttons = [[InlineKeyboardButton("🚫 Not Available 🚫", callback_data=f"hm_alert#{user_id}")]]
    btn = [
        *_status_rows(f"{query.message.link}")
    ]
    
    st = await bot.get_chat_member(chnl_id, query.from_user.id)
    if st.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
        user = await bot.get_users(int(user_id))
        request = _requested_name(query.message)
        await query.answer("Message sent to requester")
        await query.message.edit_text(f"<s>{html.escape(query.message.text)}</s>")
        await query.message.edit_reply_markup(InlineKeyboardMarkup(buttons))
        try:
            await bot.send_message(
                chat_id=int(user_id), 
                text=NOT_AVAILABLE_TXT.format(requested_name=request),
                reply_markup=InlineKeyboardMarkup(btn)
            )
        except UserIsBlocked:
            pass
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


@Client.on_callback_query(filters.regex(r"^uploaded"))
async def uploaded_callback(bot, query):
    """Handle 'Uploaded' response."""
    _, user_id, msg_id = query.data.split("#")
    chnl_id = query.message.chat.id
    
    buttons = [[InlineKeyboardButton("🙂 Uploaded 🙂", callback_data=f"ul_alert#{user_id}")]]
    btn = [*_status_rows(f"{query.message.link}")]
    
    st = await bot.get_chat_member(chnl_id, query.from_user.id)
    if st.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
        user = await bot.get_users(int(user_id))
        request = _requested_name(query.message)
        await query.answer("Message sent to requester")
        await query.message.edit_text(f"<s>{html.escape(query.message.text)}</s>")
        await query.message.edit_reply_markup(InlineKeyboardMarkup(buttons))
        try:
            await bot.send_message(
                chat_id=int(user_id), 
                text=UPLOADED_TXT.format(requested_name=request),
                reply_markup=InlineKeyboardMarkup(btn)
            )
        except UserIsBlocked:
            pass
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


@Client.on_callback_query(filters.regex(r"^already_available"))
async def already_available_callback(bot, query):
    """Handle 'Already Available' response."""
    _, user_id, msg_id = query.data.split("#")
    chnl_id = query.message.chat.id
    
    buttons = [[InlineKeyboardButton("🫤 Already Available 🫤", callback_data=f"aa_alert#{user_id}")]]
    btn = [
        *_status_rows(f"{query.message.link}")
    ]
    
    st = await bot.get_chat_member(chnl_id, query.from_user.id)
    if st.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
        user = await bot.get_users(int(user_id))
        request = _requested_name(query.message)
        await query.answer("Message sent to requester")
        await query.message.edit_text(f"<s>{html.escape(query.message.text)}</s>")
        await query.message.edit_reply_markup(InlineKeyboardMarkup(buttons))
        try:
            await bot.send_message(
                chat_id=int(user_id), 
                text=ALREADY_AVAILABLE_TXT.format(requested_name=request),
                reply_markup=InlineKeyboardMarkup(btn)
            )
        except UserIsBlocked:
            pass
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


@Client.on_callback_query(filters.regex(r"^upload_in"))
async def upload_in_callback(bot, query):
    """Handle 'Check Your Spelling' response."""
    _, user_id, msg_id = query.data.split("#")
    chnl_id = query.message.chat.id
    
    buttons = [[InlineKeyboardButton("⚠️ Check Your Spelling ⚠️", callback_data=f"upload_alert#{user_id}")]]
    btn = [
        *_status_rows(f"{query.message.link}")
    ]
    
    st = await bot.get_chat_member(chnl_id, query.from_user.id)
    if st.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
        user = await bot.get_users(int(user_id))
        request = _requested_name(query.message)
        await query.answer("Message sent to requester")
        await query.message.edit_text(f"<s>{html.escape(query.message.text)}</s>")
        await query.message.edit_reply_markup(InlineKeyboardMarkup(buttons))
        try:
            await bot.send_message(
                chat_id=int(user_id), 
                text=CHECK_SPELLING_TXT.format(requested_name=request),
                reply_markup=InlineKeyboardMarkup(btn)
            )
        except UserIsBlocked:
            pass
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


@Client.on_callback_query(filters.regex(r"^year"))
async def year_callback(bot, query):
    """Handle 'Tell Me Year/Language' response."""
    _, user_id, msg_id = query.data.split("#")
    chnl_id = query.message.chat.id
    
    buttons = [[InlineKeyboardButton("⚠️ Tell Me Year/Language ⚠️", callback_data=f"yrs_alert#{user_id}")]]
    btn = [
        *_status_rows(f"{query.message.link}")
    ]
    
    st = await bot.get_chat_member(chnl_id, query.from_user.id)
    if st.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
        user = await bot.get_users(int(user_id))
        request = _requested_name(query.message)
        await query.answer("Message sent to requester")
        await query.message.edit_text(f"<s>{html.escape(query.message.text)}</s>")
        await query.message.edit_reply_markup(InlineKeyboardMarkup(buttons))
        try:
            await bot.send_message(
                chat_id=int(user_id), 
                text=YEAR_LANGUAGE_TXT.format(requested_name=request),
                reply_markup=InlineKeyboardMarkup(btn)
            )
        except UserIsBlocked:
            pass
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


# ── Prompt helpers (admin is asked to type something in the request channel) ──
_WAIT_LIMIT = 200   # cap on unanswered prompts kept in memory

# Messages sent to the requester after "Uploaded / Available, Wrong Spelling"
UPLOADED_SPELLING_TXT = (
    "📌 Requested – <code>{requested_name}</code>\n\n"
    "✅ Your requested file is uploaded, please send correct spelling - "
    "<code>{correct_spelling}</code>"
)
AVAILABLE_SPELLING_TXT = (
    "📌 Requested – <code>{requested_name}</code>\n\n"
    "🫤 Your requested file is already uploaded, please send correct spelling - "
    "<code>{correct_spelling}</code>"
)


async def _is_channel_admin(bot, chat_id, user_id) -> bool:
    try:
        st = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return st.status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER)


async def _temp_notice(bot, chat_id, text: str, seconds: int = 10):
    """Short-lived message in the request channel (cleans itself up)."""
    try:
        note = await bot.send_message(chat_id, text)
    except Exception:
        return
    task = asyncio.create_task(_delete_later(note, seconds))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _open_prompt(bot, store: dict, request_msg, text: str, cancel_data: str, data: dict):
    """Post a 'send me X' prompt under the request message and start waiting for
    the admin's reply. Any older prompt for the same request is removed first."""
    chat_id = request_msg.chat.id
    for wait in (_CUSTOM_REPLY_WAIT, _WRONG_SPELL_WAIT):
        for pid, old in list(wait.items()):
            if old["request_msg"].id == request_msg.id:
                wait.pop(pid, None)
                try:
                    await bot.delete_messages(chat_id, pid)
                except Exception:
                    pass

    prompt = await bot.send_message(
        chat_id, text,
        reply_to_message_id=request_msg.id,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=cancel_data)]])
    )
    store[prompt.id] = {**data, "request_msg": request_msg, "prompt_id": prompt.id}
    while len(store) > _WAIT_LIMIT:
        store.pop(next(iter(store)))
    return prompt


@Client.on_callback_query(filters.regex(r"^spl_wrong"))
async def spl_wrong_callback(bot, query):
    """'Uploaded, Wrong Spelling' - first ask the admin: Uploaded or Available?"""
    _, user_id, msg_id = query.data.split("#")
    if not await _is_channel_admin(bot, query.message.chat.id, query.from_user.id):
        return await query.answer("⚠️ Admin only!", show_alert=True)

    buttons = [
        [InlineKeyboardButton("✅ Uploaded", callback_data=f"splw_up#{user_id}#{msg_id}"),
         InlineKeyboardButton("🫤 Available", callback_data=f"splw_av#{user_id}#{msg_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"show_options#{user_id}#{msg_id}")]
    ]
    try:
        await query.message.edit_reply_markup(InlineKeyboardMarkup(buttons))
    except RPCError:
        pass
    await query.answer("Uploaded or Available?")


@Client.on_callback_query(filters.regex(r"^splw_(up|av)#"))
async def spl_wrong_choice_callback(bot, query):
    """Admin picked Uploaded / Available - now ask for the correct spelling."""
    code, user_id, msg_id = query.data.split("#")
    kind = "uploaded" if code == "splw_up" else "available"
    if not await _is_channel_admin(bot, query.message.chat.id, query.from_user.id):
        return await query.answer("⚠️ Admin only!", show_alert=True)

    label = "✅ <b>Uploaded</b>" if kind == "uploaded" else "🫤 <b>Available</b>"
    await _open_prompt(
        bot, _WRONG_SPELL_WAIT, query.message,
        f"{label}\n✏️ <b>Reply to this message with the correct spelling</b>",
        f"cancel_wrong#{query.message.id}",
        {"user_id": int(user_id), "msg_id": int(msg_id), "kind": kind},
    )
    await query.answer()


@Client.on_callback_query(filters.regex(r"^cancel_wrong"))
async def cancel_wrong_callback(bot, query):
    """Cancel wrong spelling prompt."""
    _, req_msg_id = query.data.split("#")
    req_msg_id = int(req_msg_id)
    if not await _is_channel_admin(bot, query.message.chat.id, query.from_user.id):
        return await query.answer("⚠️ Admin only!", show_alert=True)
    prompt_id = next((pid for pid, data in _WRONG_SPELL_WAIT.items() if data["request_msg"].id == req_msg_id), None)
    if not prompt_id:
        return await query.answer("Nothing to cancel", show_alert=True)

    _WRONG_SPELL_WAIT.pop(prompt_id, None)
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer("Cancelled")


@Client.on_callback_query(filters.regex(r"^custom_reply"))
async def custom_reply_callback(bot, query):
    """Handle custom reply - prompt admin for custom message."""
    _, user_id, msg_id = query.data.split("#")
    if not await _is_channel_admin(bot, query.message.chat.id, query.from_user.id):
        return await query.answer("⚠️ Admin only!", show_alert=True)

    await _open_prompt(
        bot, _CUSTOM_REPLY_WAIT, query.message,
        "💬 <b>Reply to this message with your custom reply for the user</b>",
        f"cancel_custom#{query.message.id}",
        {"user_id": int(user_id), "msg_id": int(msg_id)},
    )
    await query.answer()


@Client.on_callback_query(filters.regex(r"^cancel_custom"))
async def cancel_custom_callback(bot, query):
    """Cancel custom reply prompt."""
    _, req_msg_id = query.data.split("#")
    req_msg_id = int(req_msg_id)
    if not await _is_channel_admin(bot, query.message.chat.id, query.from_user.id):
        return await query.answer("⚠️ Admin only!", show_alert=True)
    prompt_id = next((pid for pid, data in _CUSTOM_REPLY_WAIT.items() if data["request_msg"].id == req_msg_id), None)
    if not prompt_id:
        return await query.answer("Nothing to cancel", show_alert=True)

    _CUSTOM_REPLY_WAIT.pop(prompt_id, None)
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer("Cancelled")


# Alert handlers for user confirmation
@Client.on_callback_query(filters.regex(r"^na_alert"))
async def na_alert_callback(bot, query):
    _, user_id = query.data.split("#")
    if str(query.from_user.id) in user_id:
        await query.answer("Sorry your request is not available, make sure it's released. If yes, then give us some time 🤗", show_alert=True)
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


@Client.on_callback_query(filters.regex(r"^hm_alert"))
async def hm_alert_callback(bot, query):
    _, user_id = query.data.split("#")
    if str(query.from_user.id) in user_id:
        await query.answer("❌ Your requested movie is not available on the internet.", show_alert=True)
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


@Client.on_callback_query(filters.regex(r"^ul_alert"))
async def ul_alert_callback(bot, query):
    _, user_id = query.data.split("#")
    if str(query.from_user.id) in user_id:
        await query.answer("Your request is uploaded", show_alert=True)
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


@Client.on_callback_query(filters.regex(r"^aa_alert"))
async def aa_alert_callback(bot, query):
    _, user_id = query.data.split("#")
    if str(query.from_user.id) in user_id:
        await query.answer("Your request is already available, you need to check first and then make a request 🤨", show_alert=True)
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


@Client.on_callback_query(filters.regex(r"^upload_alert"))
async def upload_alert_callback(bot, query):
    _, user_id = query.data.split("#")
    if str(query.from_user.id) in user_id:
        await query.answer("You undeducated, check your spelling 😑", show_alert=True)
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


@Client.on_callback_query(filters.regex(r"^yrs_alert"))
async def yrs_alert_callback(bot, query):
    _, user_id = query.data.split("#")
    if str(query.from_user.id) in user_id:
        await query.answer("Dude you need to provide more info 😑 (like : year, language, hollywood or bollywood)", show_alert=True)
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


@Client.on_callback_query(filters.regex(r"^ulws_alert"))
async def ulws_alert_callback(bot, query):
    _, user_id = query.data.split("#")
    if str(query.from_user.id) in user_id:
        await query.answer("Correct spelling provided by admin ✏️", show_alert=True)
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


@Client.on_callback_query(filters.regex(r"^responded_alert"))
async def responded_alert_callback(bot, query):
    _, user_id = query.data.split("#")
    if str(query.from_user.id) in user_id:
        await query.answer("Admin has replied to your request 💬", show_alert=True)
    else:
        await query.answer("⚠️ Admin only!", show_alert=True)


# The prompts above are posted in the REQUEST CHANNEL (replying to the request
# message), so the admin's answer arrives there too - not in a private chat.
# (The old filter only looked at private chats, so replies were never seen.)
# Only a reply to one of THIS plugin's prompts is handled; every other message
# is left alone so nothing else in the bot is affected.
def _is_request_channel(_, __, message):
    return bool(REQUEST_CHANNEL) and message.chat is not None and message.chat.id == REQUEST_CHANNEL


request_channel_filter = filters.create(_is_request_channel)


async def _sender_is_admin(bot, message) -> bool:
    """Anyone who can post in a channel is already an admin. For a group-type
    request chat, check the actual sender."""
    user = message.from_user
    if user is None:
        # posted as the channel / anonymous admin itself
        return message.sender_chat is None or message.sender_chat.id == message.chat.id
    if user.id in ADMINS:
        return True
    return await _is_channel_admin(bot, message.chat.id, user.id)


async def _deliver(bot, user_id: int, text: str, markup) -> bool:
    try:
        await bot.send_message(chat_id=user_id, text=text, reply_markup=markup)
        return True
    except UserIsBlocked:
        return False
    except RPCError:
        logger.exception("Could not deliver request reply to %s", user_id)
        return False


async def _finish_request_message(bot, req_msg, label: str, alert_data: str):
    """Strike through the request and replace its buttons with a status button."""
    try:
        await bot.edit_message_text(
            req_msg.chat.id, req_msg.id,
            f"<s>{html.escape(req_msg.text)}</s>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=alert_data)]])
        )
    except RPCError:
        logger.debug("Could not edit request message %s", req_msg.id)


@Client.on_message(request_channel_filter & filters.reply & filters.text, group=-2)
async def handle_custom_reply_input(bot, message):
    """Handle the admin's custom reply / correct-spelling answer."""
    prompt_id = message.reply_to_message_id
    if prompt_id not in _CUSTOM_REPLY_WAIT and prompt_id not in _WRONG_SPELL_WAIT:
        return      # ordinary message in the channel - not ours
    if not await _sender_is_admin(bot, message):
        return

    try:
        typed = message.text.strip()

        # ── custom reply ────────────────────────────────────────────────
        if prompt_id in _CUSTOM_REPLY_WAIT:
            data = _CUSTOM_REPLY_WAIT.pop(prompt_id)
            req_msg = data["request_msg"]
            user_id = data["user_id"]

            delivered = await _deliver(
                bot, user_id,
                CUSTOM_REPLY_TXT.format(
                    requested_name=_requested_name(req_msg),
                    custom_message=html.escape(typed),
                ),
                InlineKeyboardMarkup([*_status_rows(req_msg.link)])
            )
            status_label, alert = "💬 Admin Replied 💬", f"responded_alert#{user_id}"

        # ── wrong spelling (uploaded / available) ───────────────────────
        else:
            data = _WRONG_SPELL_WAIT.pop(prompt_id)
            req_msg = data["request_msg"]
            user_id = data["user_id"]
            is_uploaded = data.get("kind", "uploaded") == "uploaded"

            template = UPLOADED_SPELLING_TXT if is_uploaded else AVAILABLE_SPELLING_TXT
            delivered = await _deliver(
                bot, user_id,
                template.format(
                    requested_name=_requested_name(req_msg),
                    correct_spelling=html.escape(typed),
                ),
                InlineKeyboardMarkup([*_status_rows(req_msg.link)])
            )
            status_label = "✅ Uploaded, Spelling ✏️" if is_uploaded else "🫤 Available, Spelling ✏️"
            alert = f"ulws_alert#{user_id}"

        # Clean the channel: remove the admin's typed reply and the bot's prompt.
        try:
            await bot.delete_messages(message.chat.id, [message.id, prompt_id])
        except Exception:
            pass

        await _finish_request_message(bot, req_msg, status_label, alert)

        if not delivered:
            await _temp_notice(
                bot, message.chat.id,
                "⚠️ Couldn't deliver the message - the user has blocked the bot or never started it."
            )
    finally:
        message.stop_propagation()   # we own this reply; nothing else should react to it


@Client.on_message(filters.command("req"))
@Client.on_message(filters.command("request"))
async def manual_request_cmd(bot, message):
    """
    Manual request command - /req or /request <file_name>
    Allows users to manually request a file.
    """
    if not REQUEST_CHANNEL:
        await message.reply_text(REQUEST_NOT_CONFIGURED_TXT)
        return
    
    if not message.from_user:  # anonymous group admin
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        # Half-typed command: show the format, and clear it on the same timer as
        # the search messages (/settings -> Query Auto-Delete) so chats stay clean.
        settings = await get_settings()
        text = REQUEST_HELP_TXT
        autodelete = settings["query_autodelete_enabled"]
        seconds = settings["query_autodelete_seconds"]
        if autodelete:
            text += QUERY_AUTODELETE_NOTE.format(duration=format_duration(seconds))
        help_msg = await message.reply_text(text)
        if autodelete:
            task = asyncio.create_task(_delete_later(help_msg, seconds))
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
        return
    
    query = args[1].strip()
    user = message.from_user
    username = user.username or user.first_name
    
    sent = await send_request(
        bot,
        user_id=user.id,
        query=query,
        username=username,
        origin_message=message
    )
    
    if sent:
        await message.reply_text(
            REQUEST_SENT_TXT.format(query=html.escape(query)),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ View Your Request ✨", url=sent.link)]
            ])
        )
    else:
        await message.reply_text("❌ Failed to send request. Please try again later.")
