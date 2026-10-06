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
from pyrogram.errors import UserIsBlocked

from config import ADMINS, REQUEST_CHANNEL
from database.request_db import add_request
from database.settings_db import get_settings
from utils import format_duration
from strings import (
    REQUEST_SENT_TXT, REQUEST_RECEIVED_TXT, REQUEST_NOT_CONFIGURED_TXT, REQUEST_HELP_TXT,
    QUERY_AUTODELETE_NOTE,
    ALREADY_AVAILABLE_TXT, NOT_RELEASED_TXT, CHECK_SPELLING_TXT, UPLOADED_TXT,
    NOT_AVAILABLE_TXT, YEAR_LANGUAGE_TXT, WRONG_SPELLING_TXT, CUSTOM_REPLY_TXT
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
        [InlineKeyboardButton("♻️ View Status ♻️", url=f"{query.message.link}")]
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
        [InlineKeyboardButton("♻️ View Status ♻️", url=f"{query.message.link}")]
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
    btn = [[InlineKeyboardButton("♻️ View Status ♻️", url=f"{query.message.link}")]]
    
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
        [InlineKeyboardButton("♻️ View Status ♻️", url=f"{query.message.link}")]
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
        [InlineKeyboardButton("♻️ View Status ♻️", url=f"{query.message.link}")]
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
        [InlineKeyboardButton("♻️ View Status ♻️", url=f"{query.message.link}")]
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


@Client.on_callback_query(filters.regex(r"^spl_wrong"))
async def spl_wrong_callback(bot, query):
    """Handle 'Uploaded, Wrong Spelling' - prompt admin for correct spelling."""
    _, user_id, msg_id = query.data.split("#")
    chnl_id = query.message.chat.id
    st = await bot.get_chat_member(chnl_id, query.from_user.id)
    if st.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
        return await query.answer("⚠️ Admin only!", show_alert=True)
    
    prompt = await bot.send_message(
        chnl_id, "✏️ <b>Send correct spelling</b>",
        reply_to_message_id=query.message.id,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_wrong#{query.message.id}")]])
    )
    _WRONG_SPELL_WAIT[prompt.id] = {
        "user_id": int(user_id), "msg_id": int(msg_id),
        "request_msg": query.message, "prompt_id": prompt.id
    }
    await query.answer()


@Client.on_callback_query(filters.regex(r"^cancel_wrong"))
async def cancel_wrong_callback(bot, query):
    """Cancel wrong spelling prompt."""
    _, req_msg_id = query.data.split("#")
    req_msg_id = int(req_msg_id)
    prompt_id = next((pid for pid, data in _WRONG_SPELL_WAIT.items() if data["request_msg"].id == req_msg_id), None)
    if not prompt_id:
        return await query.answer("Nothing to cancel", show_alert=True)
    
    data = _WRONG_SPELL_WAIT.pop(prompt_id)
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer("Cancelled")


@Client.on_callback_query(filters.regex(r"^custom_reply"))
async def custom_reply_callback(bot, query):
    """Handle custom reply - prompt admin for custom message."""
    _, user_id, msg_id = query.data.split("#")
    chnl_id = query.message.chat.id
    
    try:
        st = await bot.get_chat_member(chnl_id, query.from_user.id)
        if st.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            return await query.answer("⚠️ Admin only!", show_alert=True)
    except Exception:
        return await query.answer("⚠️ You are not a member of this channel, first join", show_alert=True)
    
    prompt = await bot.send_message(
        chnl_id, "💬 <b>Send your custom reply message for the user</b>",
        reply_to_message_id=query.message.id,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_custom#{query.message.id}")]])
    )
    _CUSTOM_REPLY_WAIT[prompt.id] = {
        "user_id": int(user_id), "msg_id": int(msg_id),
        "request_msg": query.message, "prompt_id": prompt.id
    }
    await query.answer()


@Client.on_callback_query(filters.regex(r"^cancel_custom"))
async def cancel_custom_callback(bot, query):
    """Cancel custom reply prompt."""
    _, req_msg_id = query.data.split("#")
    req_msg_id = int(req_msg_id)
    prompt_id = next((pid for pid, data in _CUSTOM_REPLY_WAIT.items() if data["request_msg"].id == req_msg_id), None)
    if not prompt_id:
        return await query.answer("Nothing to cancel", show_alert=True)
    
    data = _CUSTOM_REPLY_WAIT.pop(prompt_id)
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


# Only fire when an admin is actually replying to one of THIS plugin's
# prompts. A bare filters.private & filters.text also matches plain text and
# every /command — and since Pyrogram runs only the first matching handler in
# a group, that handler was registered before search.py/start.py (plugins
# load alphabetically) and silently swallowed ALL private messages, leaving
# the bot looking completely dead (no /start, no search, no /m).
def _is_prompt_reply(_, __, message):
    if not (message.chat and message.chat.type == enums.ChatType.PRIVATE):
        return False
    if not message.text:
        return False
    if not _CUSTOM_REPLY_WAIT and not _WRONG_SPELL_WAIT:
        return False
    if not message.reply_to_message:
        return False
    prompt_id = message.reply_to_message.id
    in_custom = any(d["prompt_id"] == prompt_id for d in _CUSTOM_REPLY_WAIT.values())
    in_wrong = any(d["prompt_id"] == prompt_id for d in _WRONG_SPELL_WAIT.values())
    return in_custom or in_wrong


prompt_reply_filter = filters.create(_is_prompt_reply)


@Client.on_message(prompt_reply_filter)
async def handle_custom_reply_input(bot, message):
    """Handle custom reply and wrong spelling input from admins."""
    # Check if this is a response to a custom reply prompt
    for prompt_id, data in list(_CUSTOM_REPLY_WAIT.items()):
        if message.reply_to_message and message.reply_to_message.id == data["prompt_id"]:
            user_id = data["user_id"]
            msg_id = data["msg_id"]
            req_msg = data["request_msg"]
            
            buttons = [[InlineKeyboardButton("💬 Admin Replied 💬", callback_data=f"responded_alert#{user_id}")]]
            btn = [[InlineKeyboardButton("♻️ View Status ♻️", url=f"{req_msg.link}")]]
            
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=CUSTOM_REPLY_TXT.format(
                        requested_name=_requested_name(req_msg),
                        custom_message=html.escape(message.text),
                    ),
                    reply_markup=InlineKeyboardMarkup(btn)
                )
            except UserIsBlocked:
                pass
            
            await message.delete()
            try:
                await req_msg.edit_text(f"<s>{html.escape(req_msg.text)}</s>")
                await req_msg.edit_reply_markup(InlineKeyboardMarkup(buttons))
            except Exception:
                pass
            
            _CUSTOM_REPLY_WAIT.pop(prompt_id)
            return
    
    # Check if this is a response to a wrong spelling prompt
    for prompt_id, data in list(_WRONG_SPELL_WAIT.items()):
        if message.reply_to_message and message.reply_to_message.id == data["prompt_id"]:
            user_id = data["user_id"]
            msg_id = data["msg_id"]
            req_msg = data["request_msg"]
            
            buttons = [[InlineKeyboardButton("✏️ Correct Spelling ✏️", callback_data=f"ulws_alert#{user_id}")]]
            btn = [[InlineKeyboardButton("♻️ View Status ♻️", url=f"{req_msg.link}")]]
            
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=WRONG_SPELLING_TXT.format(
                        requested_name=_requested_name(req_msg),
                        correct_spelling=html.escape(message.text),
                    ),
                    reply_markup=InlineKeyboardMarkup(btn)
                )
            except UserIsBlocked:
                pass
            
            await message.delete()
            try:
                await req_msg.edit_text(f"<s>{html.escape(req_msg.text)}</s>")
                await req_msg.edit_reply_markup(InlineKeyboardMarkup(buttons))
            except Exception:
                pass
            
            _WRONG_SPELL_WAIT.pop(prompt_id)
            return


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
