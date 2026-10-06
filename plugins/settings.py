import asyncio
import html
import logging
import os
import time

from pyrogram import Client, filters, enums
from pyrogram.errors import RPCError, UserNotParticipant, ListenerTimeout, ListenerStopped
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMINS, MOVIE_UPDATE_CHANNEL
from database.settings_db import (
    get_settings, update_settings, set_shortener, set_tutorial,
    add_fsub_channel, remove_fsub_channel,
    add_index_channel, remove_index_channel,
    add_fetch_channel, remove_fetch_channel,
)
from database.premium_db import list_premium, count_premium
from database.filters_db import count_by_channel, export_all_captions, backfill_word_index
from plugins.force_sub import is_bot_admin_in
from shortlink import make_short_link
from utils import mask_secret, IST, readable_time, format_duration, parse_duration
from strings import (
    SETTINGS_MAIN_TXT, AUTOFILTER_INFO_TXT, PM_FILTER_INFO_TXT, WELCOME_INFO_TXT,
    FSUB_MENU_HEADER, FSUB_MENU_EMPTY, FSUB_ADD_PROMPT, FSUB_ADD_NOT_CHANNEL,
    FSUB_ADD_NOT_ADMIN, FSUB_ADD_OK, FSUB_ADD_FAILED, FSUB_REMOVED_TXT,
    PREMIUM_MENU_EMPTY, PREMIUM_MENU_ROW,
    VERIFY_MENU_HEADER, VERIFY_TIER_ROW,
    SET_SHORTENER_USAGE, SET_SHORTENER_OK, SET_SHORTENER_WARN,
    SET_VERIFY_TIME_USAGE, SET_VERIFY_TIME_OK,
    SET_TUTORIAL_USAGE, SET_TUTORIAL_OK,
    INDEX_MENU_HEADER, INDEX_MENU_EMPTY, INDEX_MENU_ROW,
    INDEX_ADD_PROMPT, INDEX_ADD_NOT_CHANNEL, INDEX_ADD_NOT_ADMIN,
    INDEX_ADD_OK, INDEX_ADD_FAILED, INDEX_REMOVED_TXT,
    ASK_NUMBER_TIMEOUT, ASK_NUMBER_RETRY, ASK_BACK_BTN,
    ASK_QUERY_DELAY_PROMPT, QUERY_DELAY_SET_TXT,
    ASK_FILE_DELAY_PROMPT, FILE_DELAY_SET_TXT,
    ASK_FILE_LIMIT_PROMPT, FILE_LIMIT_SET_TXT, FILE_LIMIT_NEEDS_VERIFY_NOTE,
    EXPORT_CAPTIONS_PREPARING, EXPORT_CAPTIONS_CAPTION_TXT, EXPORT_CAPTIONS_EMPTY_TXT,
    BACKFILL_RUNNING, BACKFILL_DONE_TXT,
    MOVIE_UPDATE_MENU_HEADER,
    MOVIE_UPDATE_FETCH_HEADER, MOVIE_UPDATE_FETCH_ROW, MOVIE_UPDATE_FETCH_EMPTY,
    MOVIE_UPDATE_FETCH_ADDED, MOVIE_UPDATE_FETCH_REMOVED,
    MOVIE_UPDATE_FETCH_PROMPT, MOVIE_UPDATE_FETCH_LEGEND, MOVIE_UPDATE_WARN_NO_POST,
    MOVIE_UPDATE_BOT_NOT_IN, MOVIE_UPDATE_BOT_NOT_ADMIN, MOVIE_UPDATE_BOT_CHECK_FAILED,
)

logger = logging.getLogger(__name__)

_PREMIUM_PAGE_SIZE = 10


# ══════════════════════════════════════════════════════════════════════════════
# Menu builders — pure rendering, no side effects
# ══════════════════════════════════════════════════════════════════════════════

def _status(flag: bool) -> str:
    return "✅ ON" if flag else "❌ OFF"


def build_main_menu(settings: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_status(settings["autofilter_enabled"]), callback_data="cfg#tg#autofilter"),
         InlineKeyboardButton("🤖 Autofilter", callback_data="cfg#info#autofilter")],
        [InlineKeyboardButton(_status(settings["pm_filter_enabled"]), callback_data="cfg#tg#pmfilter"),
         InlineKeyboardButton("📩 PM Filter", callback_data="cfg#info#pmfilter")],
        [InlineKeyboardButton(_status(settings["welcome_enabled"]), callback_data="cfg#tg#welcome"),
         InlineKeyboardButton("👋 Welcome Msg", callback_data="cfg#info#welcome")],
        [InlineKeyboardButton("📚 Index", callback_data="cfg#m#index"),
         InlineKeyboardButton("➕ Add Channel", callback_data="cfg#idx_add#main")],
        [InlineKeyboardButton(_status(settings["force_sub_enabled"]), callback_data="cfg#tg#fsub"),
         InlineKeyboardButton("📢 Force-Subscribe", callback_data="cfg#m#fsub")],
        [InlineKeyboardButton(_status(settings["premium_enabled"]), callback_data="cfg#tg#premium"),
         InlineKeyboardButton("💎 Premium", callback_data="cfg#m#premium")],
        [InlineKeyboardButton(_status(settings["verify_enabled"]), callback_data="cfg#tg#verify"),
         InlineKeyboardButton("🔗 Verification", callback_data="cfg#m#verify")],
        [InlineKeyboardButton(
            "🔘 Button" if settings["result_mode"] == "button" else "📝 Text",
            callback_data="cfg#tg#resmode",
         ),
         InlineKeyboardButton("📄 Result Format", callback_data="cfg#info#resmode")],
        [InlineKeyboardButton(_status(settings["query_autodelete_enabled"]), callback_data="cfg#tg#query_ad"),
         InlineKeyboardButton(
             f"⏳ Query Auto-Delete ({readable_time(settings['query_autodelete_seconds'])})",
             callback_data="cfg#ask#query_delay",
         )],
        [InlineKeyboardButton(_status(settings["file_autodelete_enabled"]), callback_data="cfg#tg#file_ad"),
         InlineKeyboardButton(
             f"🗑 File Auto-Delete ({readable_time(settings['file_autodelete_seconds'])})",
             callback_data="cfg#ask#file_delay",
         )],
        [InlineKeyboardButton(_status(settings["file_limit_enabled"]), callback_data="cfg#tg#filelimit"),
         InlineKeyboardButton(
             f"🔢 File Limit ({settings['file_limit_count']}/day)",
             callback_data="cfg#ask#file_limit",
         )],
        [InlineKeyboardButton(_status(settings["movie_update_notification"]), callback_data="cfg#tg#movieupd"),
         InlineKeyboardButton("🎬 Movie Updates", callback_data="cfg#m#movieupd")],
        [InlineKeyboardButton("📄 Export All Captions", callback_data="cfg#export")],
        [InlineKeyboardButton("🔁 Backfill Search Index", callback_data="cfg#backfill")],
        [InlineKeyboardButton("❌ Close", callback_data="cfg#close")],
    ])


async def build_fsub_menu(bot, settings: dict):
    channels = settings["fsub_channels"]
    rows = []
    if channels:
        async def _title(cid):
            try:
                return (await bot.get_chat(cid)).title
            except RPCError:
                return str(cid)
        titles = await asyncio.gather(*[_title(c) for c in channels])
        for cid, title in zip(channels, titles):
            rows.append([InlineKeyboardButton(f"🗑 {title}", callback_data=f"cfg#fsub_rm#{cid}")])
    rows.append([InlineKeyboardButton("➕ Add New Channel", callback_data="cfg#fsub_add")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="cfg#main")])
    text = FSUB_MENU_HEADER if channels else FSUB_MENU_EMPTY
    return text, InlineKeyboardMarkup(rows)


async def build_index_menu(bot, settings: dict):
    channels = settings["index_channels"]
    rows = []
    if channels:
        async def _info(cid):
            try:
                title = (await bot.get_chat(cid)).title
            except RPCError:
                title = str(cid)
            count = await count_by_channel(cid)
            return title, count
        infos = await asyncio.gather(*[_info(c) for c in channels])
        for cid, (title, count) in zip(channels, infos):
            rows.append([InlineKeyboardButton(
                INDEX_MENU_ROW.format(title=title, count=count), callback_data=f"cfg#idx_rm#{cid}"
            )])
    rows.append([InlineKeyboardButton("➕ Add New Channel", callback_data="cfg#idx_add")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="cfg#main")])
    text = INDEX_MENU_HEADER if channels else INDEX_MENU_EMPTY
    return text, InlineKeyboardMarkup(rows)


async def build_premium_menu(offset: int = 0):
    docs = await list_premium(skip=offset, limit=_PREMIUM_PAGE_SIZE)
    total = await count_premium()

    if not docs:
        text = PREMIUM_MENU_EMPTY
    else:
        rows_txt = "".join(
            PREMIUM_MENU_ROW.format(
                user_id=d["_id"],
                expiry=d["expiry_time"].astimezone(IST).strftime("%d %b %Y, %I:%M %p"),
            )
            for d in docs
        )
        text = f"💎 <b>Premium Users</b> ({total})\n\n{rows_txt}"

    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"cfg#prem_pg#{max(0, offset - _PREMIUM_PAGE_SIZE)}"))
    if offset + _PREMIUM_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"cfg#prem_pg#{offset + _PREMIUM_PAGE_SIZE}"))

    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="cfg#main")])
    return text, InlineKeyboardMarkup(rows)


async def _bot_channel_access(bot, chat_id):
    """Check the bot's real standing in a channel.
    Returns (state, member, error): state is "ok" | "not_member" | "not_admin" | "error"."""
    try:
        member = await bot.get_chat_member(chat_id, bot.me.id)
    except UserNotParticipant:
        return "not_member", None, None
    except RPCError as exc:
        return "error", None, exc
    if member.status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
        return "ok", member, None
    return "not_admin", member, None


async def _verify_fetch_channel(bot, chat):
    """Used when an admin adds a fetch channel: returns an error text, or None if fine."""
    title = html.escape(chat.title or str(chat.id))
    state, _, error = await _bot_channel_access(bot, chat.id)
    if state == "not_member":
        return MOVIE_UPDATE_BOT_NOT_IN.format(title=title)
    if state == "not_admin":
        return MOVIE_UPDATE_BOT_NOT_ADMIN.format(title=title)
    if state == "error":
        return MOVIE_UPDATE_BOT_CHECK_FAILED.format(title=title, error=html.escape(str(error)))
    return None


async def _can_post_in_update_channel(bot) -> bool:
    if not MOVIE_UPDATE_CHANNEL:
        return False
    state, member, _ = await _bot_channel_access(bot, MOVIE_UPDATE_CHANNEL)
    if state != "ok":
        return False
    if member.status == enums.ChatMemberStatus.OWNER:
        return True
    return bool(member.privileges and member.privileges.can_post_messages)


async def build_movie_update_menu(bot, settings: dict):
    fetch_channels = list(settings.get("fetch_movie_update_channels", []))

    async def _info(cid):
        try:
            title = (await bot.get_chat(cid)).title or str(cid)
        except Exception:      # PeerIdInvalid / ChannelInvalid / etc. — never break the menu
            title = str(cid)
        state, _, _ = await _bot_channel_access(bot, cid)
        return title, state == "ok"

    infos = await asyncio.gather(*[_info(c) for c in fetch_channels])
    can_post = await _can_post_in_update_channel(bot)

    enabled = settings.get("movie_update_notification", True)
    text = MOVIE_UPDATE_MENU_HEADER.format(status=_status(enabled))
    if fetch_channels:
        rows_txt = "".join(
            MOVIE_UPDATE_FETCH_ROW.format(icon="✅" if ok else "⚠️", title=html.escape(t))
            for t, ok in infos
        )
        text += "\n\n" + MOVIE_UPDATE_FETCH_HEADER + rows_txt
        if not all(ok for _, ok in infos):
            text += MOVIE_UPDATE_FETCH_LEGEND
    else:
        text += "\n\n" + MOVIE_UPDATE_FETCH_EMPTY
    if not can_post:
        text += MOVIE_UPDATE_WARN_NO_POST

    rows = [[InlineKeyboardButton(f"{_status(enabled)} — tap to toggle", callback_data="cfg#mu_tg")]]
    rows += [
        [InlineKeyboardButton(f"🗑 {'' if ok else '⚠️ '}{t}", callback_data=f"cfg#mu_rm#{cid}")]
        for cid, (t, ok) in zip(fetch_channels, infos)
    ]
    rows.append([InlineKeyboardButton("➕ Add Fetch Channel", callback_data="cfg#mu_add")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="cfg#main")])
    return text, InlineKeyboardMarkup(rows)


def build_verify_menu(settings: dict):
    body = "\n".join(
        VERIFY_TIER_ROW.format(
            tier=tier,
            domain=settings["shorteners"].get(str(tier), {}).get("domain") or "—",
            api=mask_secret(settings["shorteners"].get(str(tier), {}).get("api", "")),
            tutorial=settings["tutorials"].get(str(tier)) or "—",
        )
        for tier in (1, 2, 3)
    )
    body += f"\n⏱ Gap 1→2: <code>{settings['verify_time']}s</code>\n⏱ Gap 2→3: <code>{settings['third_verify_time']}s</code>"
    text = VERIFY_MENU_HEADER.format(body=body)
    rows = [[InlineKeyboardButton("⬅️ Back", callback_data="cfg#main")]]
    return text, InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════════════════════════════════════════
# /settings entrypoint
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.command("settings") & filters.private & filters.user(ADMINS))
async def settings_cmd(_, message):
    settings = await get_settings()
    await message.reply_text(SETTINGS_MAIN_TXT, reply_markup=build_main_menu(settings))


@Client.on_callback_query(filters.regex(r"^cfg#") & filters.user(ADMINS))
async def settings_callback(bot, query):
    parts = query.data.split("#")
    action = parts[1]

    if action == "close":
        await query.answer()
        try:
            await query.message.delete()
        except RPCError:
            pass
        return

    if action == "main":
        settings = await get_settings()
        await query.answer()
        await query.message.edit_text(SETTINGS_MAIN_TXT, reply_markup=build_main_menu(settings))
        return

    if action == "cancel":
        # Back/Cancel pressed on an "send me a value" prompt: stop waiting for the
        # admin's reply; the waiting task (below) then redraws the right panel.
        await query.answer()
        key = (query.message.chat.id, query.from_user.id)
        if key in _PENDING:
            await bot.stop_listening(chat_id=key[0], user_id=key[1])
        else:  # stale prompt (e.g. after a restart) — nothing is waiting, just redraw
            await _show_panel(bot, query.message, parts[2] if len(parts) > 2 else "main")
        return

    if action == "tg":
        field = {
            "fsub": "force_sub_enabled",
            "premium": "premium_enabled",
            "verify": "verify_enabled",
            "query_ad": "query_autodelete_enabled",
            "file_ad": "file_autodelete_enabled",
            "filelimit": "file_limit_enabled",
            "movieupd": "movie_update_notification",
            "autofilter": "autofilter_enabled",
            "pmfilter": "pm_filter_enabled",
            "welcome": "welcome_enabled",
        }.get(parts[2])
        if field:
            settings = await get_settings()
            settings = await update_settings({field: not settings[field]})
        else:  # result mode toggle
            settings = await get_settings()
            new_mode = "text" if settings["result_mode"] == "button" else "button"
            settings = await update_settings({"result_mode": new_mode})
        await query.answer()
        await query.message.edit_reply_markup(build_main_menu(settings))
        return

    if action == "info":
        info_texts = {
            "autofilter": AUTOFILTER_INFO_TXT,
            "pmfilter": PM_FILTER_INFO_TXT,
            "welcome": WELCOME_INFO_TXT,
        }
        if parts[2] in info_texts:
            await query.answer(info_texts[parts[2]], show_alert=True)
            return
        await query.answer(
            "Choose whether search results show as tappable buttons or a plain text list.",
            show_alert=True,
        )
        return

    if action == "m":
        settings = await get_settings()
        await query.answer()
        if parts[2] == "fsub":
            text, markup = await build_fsub_menu(bot, settings)
        elif parts[2] == "index":
            text, markup = await build_index_menu(bot, settings)
        elif parts[2] == "premium":
            text, markup = await build_premium_menu()
        elif parts[2] == "movieupd":
            text, markup = await build_movie_update_menu(bot, settings)
        else:
            text, markup = build_verify_menu(settings)
        await query.message.edit_text(text, reply_markup=markup)
        return

    if action == "prem_pg":
        offset = int(parts[2])
        text, markup = await build_premium_menu(offset)
        await query.answer()
        await query.message.edit_text(text, reply_markup=markup)
        return

    if action == "fsub_rm":
        channel_id = int(parts[2])
        settings = await remove_fsub_channel(channel_id)
        await query.answer(FSUB_REMOVED_TXT)
        text, markup = await build_fsub_menu(bot, settings)
        await query.message.edit_text(text, reply_markup=markup)
        return

    if action == "fsub_add":
        await query.answer()
        await _run_channel_add(
            bot, query,
            prompt_text=FSUB_ADD_PROMPT, not_channel_text=FSUB_ADD_NOT_CHANNEL,
            not_admin_text=FSUB_ADD_NOT_ADMIN, ok_text=FSUB_ADD_OK, failed_text=FSUB_ADD_FAILED,
            add_fn=add_fsub_channel, build_menu_fn=build_fsub_menu, back_to="fsub",
        )
        return

    if action == "idx_rm":
        channel_id = int(parts[2])
        settings = await remove_index_channel(channel_id)
        await query.answer(INDEX_REMOVED_TXT)
        text, markup = await build_index_menu(bot, settings)
        await query.message.edit_text(text, reply_markup=markup)
        return

    if action == "idx_add":
        await query.answer()
        # "cfg#idx_add#main" = pressed on the main panel, plain "cfg#idx_add" = on the Index panel
        back_to = "main" if len(parts) > 2 and parts[2] == "main" else "index"
        await _run_channel_add(
            bot, query,
            prompt_text=INDEX_ADD_PROMPT, not_channel_text=INDEX_ADD_NOT_CHANNEL,
            not_admin_text=INDEX_ADD_NOT_ADMIN, ok_text=INDEX_ADD_OK, failed_text=INDEX_ADD_FAILED,
            add_fn=add_index_channel, build_menu_fn=build_index_menu, back_to=back_to,
        )
        return

    if action == "mu_add":
        await query.answer()
        await _run_channel_add(
            bot, query,
            prompt_text=MOVIE_UPDATE_FETCH_PROMPT, not_channel_text=INDEX_ADD_NOT_CHANNEL,
            not_admin_text=INDEX_ADD_NOT_ADMIN, ok_text=MOVIE_UPDATE_FETCH_ADDED,
            failed_text=INDEX_ADD_FAILED,
            add_fn=add_fetch_channel, build_menu_fn=build_movie_update_menu,
            verify_fn=_verify_fetch_channel, back_to="movieupd",
        )
        return

    if action == "mu_rm":
        channel_id = int(parts[2])
        settings = await remove_fetch_channel(channel_id)
        await query.answer(MOVIE_UPDATE_FETCH_REMOVED)
        text, markup = await build_movie_update_menu(bot, settings)
        await query.message.edit_text(text, reply_markup=markup)
        return

    if action == "mu_tg":
        settings = await get_settings()
        settings = await update_settings(
            {"movie_update_notification": not settings.get("movie_update_notification", True)}
        )
        await query.answer()
        text, markup = await build_movie_update_menu(bot, settings)
        await query.message.edit_text(text, reply_markup=markup)
        return

    if action == "ask":
        await query.answer()
        await _run_ask_number(bot, query, parts[2])
        return

    if action == "export":
        await query.answer(EXPORT_CAPTIONS_PREPARING)
        path = f"/tmp/captions_export_{int(time.time())}.txt"
        try:
            count = await export_all_captions(path)
            if count == 0:
                await query.message.reply_text(EXPORT_CAPTIONS_EMPTY_TXT)
                return
            await query.message.reply_document(
                path, caption=EXPORT_CAPTIONS_CAPTION_TXT.format(count=count),
            )
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return

    if action == "backfill":
        await query.answer(BACKFILL_RUNNING)
        updated = await backfill_word_index()
        await query.message.reply_text(BACKFILL_DONE_TXT.format(count=updated))
        return


async def _resolve_channel(bot, reply):
    """Turn a listened-for reply (forwarded message or @username/-100 id)
    into a Chat, or None if it can't be resolved."""
    if reply.forward_from_chat and reply.forward_from_chat.type == enums.ChatType.CHANNEL:
        return reply.forward_from_chat, None
    if reply.text:
        text = reply.text.strip()
        try:
            target = int(text) if text.lstrip("-").isdigit() else text
            return await bot.get_chat(target), None
        except RPCError as exc:
            return None, exc
    return None, None


# ── "send me a value" prompts ─────────────────────────────────────────────────
# While a prompt is open the admin sees one message with a Back button. Their
# typed reply is read and then deleted, so the chat stays clean. Back (or any
# /command, or 2 minutes of silence) stops the wait and redraws the panel.

_PENDING: dict = {}          # (chat_id, user_id) -> True while a prompt is waiting


def _back_markup(where: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(ASK_BACK_BTN, callback_data=f"cfg#cancel#{where}")]])


async def _show_panel(bot, message, where: str, prefix: str = ""):
    """Redraw the settings panel `where` (main | fsub | index | movieupd) in `message`."""
    settings = await get_settings()
    if where == "fsub":
        text, markup = await build_fsub_menu(bot, settings)
    elif where == "index":
        text, markup = await build_index_menu(bot, settings)
    elif where == "movieupd":
        text, markup = await build_movie_update_menu(bot, settings)
    else:
        text, markup = SETTINGS_MAIN_TXT, build_main_menu(settings)
    try:
        await message.edit_text((prefix + "\n\n" if prefix else "") + text, reply_markup=markup)
    except RPCError:
        pass


async def _safe_delete(message):
    try:
        await message.delete()
    except Exception:
        pass


async def _wait_for_admin(bot, chat_id: int, user_id: int, timeout: float):
    """-> (message, None) or (None, "timeout" | "cancel")."""
    key = (chat_id, user_id)
    _PENDING[key] = True
    try:
        return await bot.listen(chat_id=chat_id, user_id=user_id, timeout=timeout), None
    except ListenerTimeout:
        return None, "timeout"
    except ListenerStopped:
        return None, "cancel"
    finally:
        _PENDING.pop(key, None)


async def _run_channel_add(bot, query, *, prompt_text, not_channel_text, not_admin_text,
                            ok_text, failed_text, add_fn, build_menu_fn, verify_fn=None,
                            back_to="main"):
    prompt = await query.message.edit_text(prompt_text, reply_markup=_back_markup(back_to))
    reply, outcome = await _wait_for_admin(bot, query.message.chat.id, query.from_user.id, 90)
    if outcome == "cancel":
        await _show_panel(bot, prompt, back_to)
        return
    if outcome == "timeout":
        await _show_panel(bot, prompt, back_to, ASK_NUMBER_TIMEOUT)
        return

    if reply.text and reply.text.startswith("/"):      # a command = "never mind"
        await _safe_delete(reply)
        await _show_panel(bot, prompt, back_to)
        return

    chat, error = await _resolve_channel(bot, reply)
    await _safe_delete(reply)                          # read it, then remove it
    settings = await get_settings()

    if error:
        menu_text, markup = await build_menu_fn(bot, settings)
        await prompt.edit_text(failed_text.format(error=error) + "\n\n" + menu_text, reply_markup=markup)
        return

    if not chat or chat.type != enums.ChatType.CHANNEL:
        text, markup = await build_menu_fn(bot, settings)
        await prompt.edit_text(not_channel_text + "\n\n" + text, reply_markup=markup)
        return

    if verify_fn:
        problem = await verify_fn(bot, chat)
    else:
        problem = None if await is_bot_admin_in(bot, chat.id) else not_admin_text
    if problem:
        text, markup = await build_menu_fn(bot, settings)
        await prompt.edit_text(problem + "\n\n" + text, reply_markup=markup)
        return

    settings = await add_fn(chat.id)
    text, markup = await build_menu_fn(bot, settings)
    await prompt.edit_text(ok_text.format(title=html.escape(chat.title or str(chat.id))) + "\n\n" + text, reply_markup=markup)


def _parse_ask_value(field: str, raw: str):
    """Delay fields take seconds ("300") or a unit ("5min", "2hours", "1day");
    the file limit takes a plain number. None = invalid."""
    if raw.isdigit():
        return int(raw)
    if field in ("query_delay", "file_delay"):
        return parse_duration(raw)
    return None


async def _run_ask_number(bot, query, field: str):
    prompts = {
        "query_delay": ASK_QUERY_DELAY_PROMPT,
        "file_delay": ASK_FILE_DELAY_PROMPT,
        "file_limit": ASK_FILE_LIMIT_PROMPT,
    }
    markup = _back_markup("main")
    prompt = await query.message.edit_text(prompts[field], reply_markup=markup)

    deadline = time.monotonic() + 120
    value = None
    while value is None:
        reply, outcome = await _wait_for_admin(
            bot, query.message.chat.id, query.from_user.id, max(deadline - time.monotonic(), 1),
        )
        if outcome == "cancel":
            await _show_panel(bot, prompt, "main")
            return
        if outcome == "timeout":
            await _show_panel(bot, prompt, "main", ASK_NUMBER_TIMEOUT)
            return

        raw = (reply.text or "").strip()
        await _safe_delete(reply)                      # read it, then remove it
        if raw.startswith("/"):                        # a command = "never mind"
            await _show_panel(bot, prompt, "main")
            return
        value = _parse_ask_value(field, raw)
        if value is None:                              # stay on the prompt, let them retry
            try:
                await prompt.edit_text(ASK_NUMBER_RETRY + prompts[field], reply_markup=markup)
            except RPCError:
                pass

    if field == "query_delay":
        settings = await update_settings({"query_autodelete_seconds": value})
        text = QUERY_DELAY_SET_TXT.format(duration=format_duration(value))
    elif field == "file_delay":
        settings = await update_settings({"file_autodelete_seconds": value})
        text = FILE_DELAY_SET_TXT.format(duration=format_duration(value))
    else:
        settings = await update_settings({"file_limit_count": value})
        text = FILE_LIMIT_SET_TXT.format(count=value)
        if not settings["verify_enabled"]:
            text += FILE_LIMIT_NEEDS_VERIFY_NOTE

    await prompt.edit_text(text, reply_markup=build_main_menu(settings))


# ══════════════════════════════════════════════════════════════════════════════
# Verification config commands (referenced from the Verification submenu)
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.command("set_shortener") & filters.private & filters.user(ADMINS))
async def set_shortener_cmd(_, message):
    parts = message.text.split(maxsplit=3)
    if len(parts) < 4 or parts[1] not in ("1", "2", "3"):
        await message.reply_text(SET_SHORTENER_USAGE)
        return
    tier, domain, api = int(parts[1]), parts[2], parts[3]
    await set_shortener(tier, domain, api)

    demo = await make_short_link("https://t.me", domain, api)
    if demo == "https://t.me":
        await message.reply_text(SET_SHORTENER_WARN)
    else:
        await message.reply_text(SET_SHORTENER_OK.format(tier=tier, demo=demo), disable_web_page_preview=True)


@Client.on_message(filters.command("set_verify_time") & filters.private & filters.user(ADMINS))
async def set_verify_time_cmd(_, message):
    parts = message.text.split()
    if len(parts) != 3 or parts[1] not in ("1", "2") or not parts[2].isdigit():
        await message.reply_text(SET_VERIFY_TIME_USAGE)
        return
    gap, seconds = parts[1], int(parts[2])
    field = "verify_time" if gap == "1" else "third_verify_time"
    await update_settings({field: seconds})
    await message.reply_text(SET_VERIFY_TIME_OK.format(gap=gap, seconds=seconds))


@Client.on_message(filters.command("set_tutorial") & filters.private & filters.user(ADMINS))
async def set_tutorial_cmd(_, message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or parts[1] not in ("1", "2", "3"):
        await message.reply_text(SET_TUTORIAL_USAGE)
        return
    tier = int(parts[1])
    await set_tutorial(tier, parts[2].strip())
    await message.reply_text(SET_TUTORIAL_OK.format(tier=tier))
