import asyncio
import hashlib
import html
import logging
import math
import re
import time
from urllib.parse import quote_plus

from pyrogram import Client, filters, enums
from pyrogram.errors import RPCError
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import RESULTS_PER_PAGE, REQUEST_CHANNEL, ADMINS, SUGGESTION_TIMEOUT, NOT_FOUND_FILE_CHANNEL, MOVIE_GROUP_LINK, is_support_chat
from database.filters_db import (
    search_files, display_name, extract_meta, apply_filters, is_language_year_query, clean_display_text,
    related_titles, relaxed_search, parse_query,
)
from database.alias_db import alias_key, create_miss, set_miss_message
from plugins.learn import note_miss, note_success, note_tap
from database.query_log_db import log_query
from database.settings_db import get_settings
from database.trending_db import record_search, title_of
from filterwords import apply_filter_words
from linkcheck import has_link
from poster import fetch_poster_for_results
from spellcheck import fuzzy_alternatives, fuzzy_correct, suggest_titles
from titleindex import button_label, buttons_for, files_for_button
from utils import temp, human_size, format_duration
from strings import (
    NOT_FOUND_TXT, RESULT_HEADER_TXT, RESULT_HEADER_CORRECTED_TXT, POSTER_CAPTION_TXT,
    SEARCH_EXPIRED_TXT, QUERY_AUTODELETE_NOTE, FILTER_LABELS, FILTER_MENU_TXT,
    FILTER_CLEAR_BTN, HOME_BTN, NO_MATCH_TXT,
    STATUS_STAGE1_TXT, STATUS_STAGE2_TXT, STATUS_STAGE3_TXT,
    SUGGESTIONS_HEADER_TXT, SUGGESTION_NOT_FOUND_TXT, RELATED_HEADER_TXT,
    SEASON_MENU_TXT, SEASON_BTN, SEASON_ALL_BTN, RELAXED_NOTE_TXT, RELAXED_AVAILABLE_TXT,
    TITLE_PICK_TXT, TITLE_MORE_BTN, TITLE_MORE_HEADER_TXT, ALT_BTN, ALT_HEADER_TXT,
    REQUEST_BTN_TXT, REQUEST_NOT_CONFIGURED_TXT, REQUEST_SENT_TXT,
    MAINTENANCE_TXT, EMPTY_QUERY_TXT,
    PM_SEARCH_OFF_TXT, PM_SEARCH_OFF_BTN,
)

logger = logging.getLogger(__name__)

TEXT_LIMIT = 4096  # Telegram's hard cap for a plain message — defensive only,
                    # real per-file entries are a few hundred chars at most so
                    # this is never expected to trigger at the default page size.

# ── caption sanitising ──────────────────────────────────────────────────────
# Some source channels bake literal HTML tags straight into the caption
# (e.g. "<b>Movie Name ...</b>") expecting Telegram to render them. That
# breaks the moment we wrap the same text inside our own <a href="...">
# link — the leftover/duplicate tags either show up as visible "<b>" text or
# make the whole entity malformed so Telegram drops the link entirely.
# Strip any tag-shaped substring first, then escape whatever plain text is
# left (handles stray "&", "<", ">" that are just part of the file name).
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?>")


def _strip_tags(text: str) -> str:
    text = _HTML_TAG_RE.sub(" ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _safe_caption(text: str) -> str:
    return html.escape(_strip_tags(text))


# ── what a file is called on the result page ────────────────────────────────
# Emojis, decorative symbols and empty brackets are removed from the file
# name / caption (see clean_display_text in database/filters_db.py); the rest
# of the name is unchanged. The stored caption and the file itself are
# untouched. Tags are stripped first so "(<b>🔥</b>)" counts as empty too.

def _doc_label(doc: dict) -> str:
    label = clean_display_text(_strip_tags(display_name(doc)))
    if not label:  # caption was nothing but emojis/symbols — use the file name
        label = clean_display_text(_strip_tags(doc.get("file_name", "") or ""))
    return label or "Unnamed file"


# ══════════════════════════════════════════════════════════════════════════════
# Per-query result cache — one DB round trip (+ one poster lookup) per search,
# pagination and filtering are both free after that. The display mode
# (button/text) is snapshotted at search time, so an admin toggling it
# mid-way doesn't mix formats within the same result set. Filter state lives
# here too, shared by anyone tapping the same result message.
# ══════════════════════════════════════════════════════════════════════════════

_CACHE: dict = {}
_CACHE_TTL = 600  # 10 minutes
_CACHE_MAX_ENTRIES = 300

_EMPTY_FILTERS = {"season": None, "language": None, "year": None, "quality": None, "episode": None}


def _cache_key(query: str) -> str:
    return hashlib.md5(query.strip().lower().encode()).hexdigest()[:12]


def _cache_get(key: str) -> dict | None:
    entry = _CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["time"] > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return entry


def _cache_put(key: str, query: str, results: list, mode: str, poster, original_query: str | None = None,
               note: str | None = None, extra_rows: list | None = None) -> dict:
    if len(_CACHE) >= _CACHE_MAX_ENTRIES:
        oldest = min(_CACHE, key=lambda k: _CACHE[k]["time"])
        _CACHE.pop(oldest, None)
    entry = {
        "query": query,
        "original_query": original_query,  # set only when a typo-correction was used
        "note": note,  # one extra line under the header (e.g. "Season 5 isn't available")
        "extra": extra_rows or [],  # extra button rows ("More titles", "Not this? See other matches")
        "results": results,
        "mode": mode,
        "meta": extract_meta(results),
        "poster": poster,
        "filters": dict(_EMPTY_FILTERS),
        "time": time.time(),
    }
    _CACHE[key] = entry
    return entry


def _fit_text(text: str, limit: int = TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _chunk(items: list, size: int) -> list:
    return [items[i:i + size] for i in range(0, len(items), size)]


# ══════════════════════════════════════════════════════════════════════════════
# Stage-3 suggestion cache — short-lived, keyed the same way as the result
# cache. Buttons only ever carry an index into this list (never the title
# text itself, which could blow past Telegram's callback_data limit), so a
# click just looks up "which title was button N" and searches that.
# ══════════════════════════════════════════════════════════════════════════════

_SUGGESTION_CACHE: dict = {}
_SUGGESTION_TRACKER: dict = {}  # Track suggestion messages for auto-request timeout


def _suggestion_cache_put(key: str, titles: list, query: str = ""):
    if len(_SUGGESTION_CACHE) >= _CACHE_MAX_ENTRIES:
        oldest = min(_SUGGESTION_CACHE, key=lambda k: _SUGGESTION_CACHE[k]["time"])
        _SUGGESTION_CACHE.pop(oldest, None)
    _SUGGESTION_CACHE[key] = {"titles": titles, "time": time.time(), "query": query}


def _suggestion_cache_get(key: str) -> list | None:
    entry = _SUGGESTION_CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["time"] > _CACHE_TTL:
        _SUGGESTION_CACHE.pop(key, None)
        return None
    return entry["titles"]


def _suggestion_keyboard(key: str, titles: list) -> InlineKeyboardMarkup:
    rows = []
    for i, title in enumerate(titles):
        label = "🎬 " + (title if len(title) <= 55 else title[:52] + "…")
        rows.append([InlineKeyboardButton(label, callback_data=f"sug#{key}#{i}")])
    return InlineKeyboardMarkup(rows)


# ── Request-to-admin button ─────────────────────────────────────────────────
# callback_data is capped at 64 bytes by Telegram, so a button can never carry
# the query text itself (long / non-ASCII / '#'-containing queries made the
# whole not-found message fail to send). It carries a short key into this
# cache instead.
_REQUEST_CACHE: dict = {}


def _request_cache_put(query: str) -> str:
    if len(_REQUEST_CACHE) >= _CACHE_MAX_ENTRIES:
        oldest = min(_REQUEST_CACHE, key=lambda k: _REQUEST_CACHE[k]["time"])
        _REQUEST_CACHE.pop(oldest, None)
    key = _cache_key(query)
    _REQUEST_CACHE[key] = {"query": query, "time": time.time()}
    return key


def _request_cache_get(key: str) -> str | None:
    entry = _REQUEST_CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["time"] > 6 * 3600:
        _REQUEST_CACHE.pop(key, None)
        return None
    return entry["query"]


def _action_rows(query: str, user_id: int | None) -> list:
    """The two buttons shown under every 'not found' / 'did you mean' message:
    Google search (opens Google with the query) + Request to Admin."""
    rows = [[InlineKeyboardButton(
        "🔍 Search on Google",
        url=f"https://www.google.com/search?q={quote_plus(query)}",
    )]]
    if REQUEST_CHANNEL and user_id:
        key = _request_cache_put(query)
        rows.append([InlineKeyboardButton(REQUEST_BTN_TXT, callback_data=f"req#{key}#{user_id}")])
    return rows


async def _schedule_auto_request(bot, status_message, query: str, user_id: int):
    """Schedule a file not found notification after timeout if user doesn't interact."""
    await asyncio.sleep(SUGGESTION_TIMEOUT)
    
    # Check if the message is still tracked (user hasn't clicked anything)
    tracker = _SUGGESTION_TRACKER.get(status_message.id)
    if tracker and not tracker["clicked"]:
        if NOT_FOUND_FILE_CHANNEL and user_id not in ADMINS:
            try:
                # Get the actual user info from the bot
                user = await bot.get_users(user_id)
                mention = f"<a href='tg://user?id={user_id}'>{html.escape(user.first_name or str(user_id))}</a>"
                text = (
                    "<b>#FILE_NOT_FOUND</b>\n\n"
                    f"👤 User: {mention}\n"
                    f"🆔 ID: <code>{user_id}</code>\n"
                    f"🔍 Query: <code>{html.escape(query)}</code>"
                )
                # Fix / Ignore buttons let an admin turn this miss into a search
                # alias (handled in plugins/alias_fix.py). If the DB write fails
                # the report is still sent, just without buttons.
                markup, miss_id = None, None
                try:
                    key = alias_key(query)
                    if key:
                        miss_id = await create_miss(query, key, user_id, text)
                        markup = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✏️ Fix", callback_data=f"nfa#fix#{miss_id}"),
                            InlineKeyboardButton("🚫 Ignore", callback_data=f"nfa#ign#{miss_id}"),
                        ]])
                except Exception:
                    logger.exception("Could not store not-found report")
                sent = await bot.send_message(chat_id=NOT_FOUND_FILE_CHANNEL, text=text, reply_markup=markup)
                if miss_id:
                    await set_miss_message(miss_id, sent.id)
            except Exception:
                pass  # Fail silently if user can't be fetched or message fails
    
    # Clean up tracker
    _SUGGESTION_TRACKER.pop(status_message.id, None)


# ══════════════════════════════════════════════════════════════════════════════
# Rendering — always a plain text message (4096-char budget). The poster, when
# found, is sent once as its own separate photo with a short static caption
# and is never touched again, so it can never run into a caption-length
# problem no matter how long the file list gets.
# ══════════════════════════════════════════════════════════════════════════════

def _filter_rows(key: str, meta: dict, filters: dict, offset: int) -> list:
    rows = []

    row1 = []
    if meta["seasons"]:
        label = f"📅 S{int(filters['season']):02d}" if filters["season"] else FILTER_LABELS["season"]
        row1.append(InlineKeyboardButton(label, callback_data=f"fm#{key}#season#{offset}"))
    if meta["languages"]:
        label = f"🌐 {filters['language'].title()}" if filters["language"] else FILTER_LABELS["language"]
        row1.append(InlineKeyboardButton(label, callback_data=f"fm#{key}#language#{offset}"))
    if meta["episodes"]:
        label = f"▶️ E{filters['episode']}" if filters["episode"] else FILTER_LABELS["episode"]
        row1.append(InlineKeyboardButton(label, callback_data=f"fm#{key}#episode#{offset}"))
    if row1:
        rows.append(row1)

    row2 = []
    if meta["years"]:
        label = f"📆 {filters['year']}" if filters["year"] else FILTER_LABELS["year"]
        row2.append(InlineKeyboardButton(label, callback_data=f"fm#{key}#year#{offset}"))
    if meta["qualities"]:
        quality_label = dict(meta["qualities"]).get(filters["quality"], "")
        label = f"🎞 {quality_label}" if filters["quality"] else FILTER_LABELS["quality"]
        row2.append(InlineKeyboardButton(label, callback_data=f"fm#{key}#quality#{offset}"))
    if row2:
        rows.append(row2)

    return rows


def _nav_row(key: str, offset: int, total: int) -> list:
    total_pages = max(1, math.ceil(total / RESULTS_PER_PAGE))
    current_page = offset // RESULTS_PER_PAGE + 1
    nav = [InlineKeyboardButton("⬅️", callback_data=f"pg#{key}#{max(0, offset - RESULTS_PER_PAGE)}")] \
        if offset > 0 else []
    nav.append(InlineKeyboardButton(f"📄 {current_page}/{total_pages}", callback_data="noop"))
    if offset + RESULTS_PER_PAGE < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"pg#{key}#{offset + RESULTS_PER_PAGE}"))
    return nav


def _render(key: str, entry: dict, offset: int):
    """Return (text, InlineKeyboardMarkup) for one page, filters applied."""
    filters_active = any(entry["filters"].values())
    filtered = apply_filters(entry["results"], entry["filters"]) if filters_active else entry["results"]
    total = len(filtered)
    if total == 0:
        offset = 0
    elif offset >= total:
        offset = ((total - 1) // RESULTS_PER_PAGE) * RESULTS_PER_PAGE
    page = filtered[offset:offset + RESULTS_PER_PAGE]
    header = (
        RESULT_HEADER_CORRECTED_TXT.format(
            query=html.escape(entry["query"]),
            original=html.escape(entry["original_query"]),
            total=total,
        )
        if entry.get("original_query")
        else RESULT_HEADER_TXT.format(query=html.escape(entry["query"]), total=total)
    )

    if entry.get("note"):
        header += "\n" + entry["note"]

    rows = _filter_rows(key, entry["meta"], entry["filters"], offset)

    if total == 0:
        if filters_active:
            rows.append([InlineKeyboardButton(HOME_BTN, callback_data=f"home#{key}")])
        return header + "\n\n" + NO_MATCH_TXT, InlineKeyboardMarkup(rows)

    if entry["mode"] == "text":
        lines = []
        for doc in page:
            label = _safe_caption(_doc_label(doc))
            url = f"https://t.me/{temp.U_NAME}?start=file_{doc['_id']}"
            lines.append(f'📁 <a href="{url}">{label}</a> • {human_size(doc.get("file_size", 0))}')
        text = header + "\n\n" + "\n\n".join(lines)
    else:
        text = header
        for doc in page:
            label = f"{_strip_tags(_doc_label(doc))} • {human_size(doc.get('file_size', 0))}"
            label = "📁 " + label
            if len(label) > 60:
                label = label[:57] + "…"
            rows.append([InlineKeyboardButton(
                label, url=f"https://t.me/{temp.U_NAME}?start=file_{doc['_id']}"
            )])

    rows.extend(entry.get("extra", []))
    if filters_active:
        rows.append([InlineKeyboardButton(HOME_BTN, callback_data=f"home#{key}")])
    rows.append(_nav_row(key, offset, total))

    return _fit_text(text), InlineKeyboardMarkup(rows)


def _filter_menu(key: str, ftype: str, entry: dict, offset: int):
    meta_field = {"season": "seasons", "language": "languages", "year": "years",
                  "quality": "qualities", "episode": "episodes"}[ftype]
    values = entry["meta"][meta_field]

    buttons = []
    for v in values:
        if ftype == "quality":
            code, label = v
            buttons.append(InlineKeyboardButton(label, callback_data=f"fv#{key}#quality#{code}#{offset}"))
        elif ftype == "season":
            buttons.append(InlineKeyboardButton(f"S{v:02d}", callback_data=f"fv#{key}#season#{v}#{offset}"))
        elif ftype == "episode":
            buttons.append(InlineKeyboardButton(f"E{v}", callback_data=f"fv#{key}#episode#{v}#{offset}"))
        elif ftype == "language":
            buttons.append(InlineKeyboardButton(v.title(), callback_data=f"fv#{key}#language#{v}#{offset}"))
        else:
            buttons.append(InlineKeyboardButton(str(v), callback_data=f"fv#{key}#year#{v}#{offset}"))

    rows = _chunk(buttons, 4)
    rows.insert(0, [InlineKeyboardButton(FILTER_CLEAR_BTN, callback_data=f"fv#{key}#{ftype}#_any_#{offset}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"rr#{key}#{offset}")])

    text = FILTER_MENU_TXT.format(label=FILTER_LABELS[ftype].split(" ", 1)[1])
    return text, InlineKeyboardMarkup(rows)


async def _push(message, text: str, markup):
    """The results message is always plain text — editing is always edit_text.
    The poster (when present) is a separate, static message and never carries
    the keyboard, so a callback can never originate from it."""
    await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)


async def _expired(message):
    await message.edit_text(SEARCH_EXPIRED_TXT)


# ══════════════════════════════════════════════════════════════════════════════
# Message handler
# ══════════════════════════════════════════════════════════════════════════════

def _search_filter(_, __, message):
    if not message.text or message.text.startswith("/"):
        return False
    if has_link(message):  # links are never a search query
        return False
    # The bot's own messages (restart notice, "Searching..." status, results)
    # come back to it as updates — they must never be treated as a query, or
    # every reply the bot sends triggers another search. Only real users and
    # admins can search.
    sender = message.from_user
    if message.outgoing or (sender and (sender.is_self or sender.is_bot)):
        return False
    # The request / not-found channels are admin work areas. If one of them is a
    # group, an admin's answer typed there ("Hindi Medium") must never start a search.
    if message.chat.id in (REQUEST_CHANNEL, NOT_FOUND_FILE_CHANNEL):
        return False
    # The support group has its own AI helper (plugins/support_group.py) — the normal
    # autofilter results are switched off there.
    if is_support_chat(message.chat):
        return False
    if message.chat.type == enums.ChatType.PRIVATE:
        # Always let a DM query through to handle_search(): whether PM search is
        # ON or OFF is an admin switch (/settings -> PM Filter) checked there, so
        # that when it is OFF the user gets the "search in group" reply.
        return True
    return message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)


search_filter = filters.create(_search_filter)


_DELETE_AT: dict = {}   # (chat_id, message_id) -> time.monotonic() deadline


async def _schedule_delete(message, seconds: int):
    key = (message.chat.id, message.id)
    if seconds > 0:
        _DELETE_AT[key] = time.monotonic() + seconds
    await asyncio.sleep(seconds)
    _DELETE_AT.pop(key, None)
    try:
        await message.delete()
    except RPCError:
        pass


def _remaining_note(message) -> str:
    """"This message will self-destruct in ..." for a message that already has a
    delete scheduled (used when its text is edited later). "" if none scheduled."""
    deadline = _DELETE_AT.get((message.chat.id, message.id))
    if not deadline:
        return ""
    left = int(deadline - time.monotonic())
    return QUERY_AUTODELETE_NOTE.format(duration=format_duration(max(left, 1)))


async def _status_update(message, text: str, markup=None):
    try:
        return await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except RPCError:
        return message


async def _deliver_results(message, resolved_query: str, results: list,
                            original_query: str | None = None, note: str | None = None,
                            extra_rows: list | None = None):
    """Send the poster (if found) + the results/filter message — the exact
    same flow whether Stage 1 found it directly or the user just clicked a
    Stage-3 suggestion button. The poster always comes from the result
    files' own names (most common title if they're mixed), never from the
    typed query."""
    poster = await fetch_poster_for_results(results)

    settings = await get_settings()
    mode = settings["result_mode"]
    key = _cache_key(resolved_query)
    entry = _cache_put(key, resolved_query, results, mode, poster, original_query, note, extra_rows)

    text, markup = _render(key, entry, offset=0)
    if settings["query_autodelete_enabled"]:
        text += QUERY_AUTODELETE_NOTE.format(duration=format_duration(settings["query_autodelete_seconds"]))

    to_delete = []
    if poster:
        poster_msg = await message.reply_photo(
            poster["url"],
            caption=POSTER_CAPTION_TXT.format(query=html.escape(poster.get("title") or resolved_query)),
            quote=True,
        )
        to_delete.append(poster_msg)
        sent = await message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)
    else:
        sent = await message.reply_text(
            text, reply_markup=markup, quote=True, disable_web_page_preview=True,
        )
    to_delete.append(sent)

    if settings["query_autodelete_enabled"]:
        for m in to_delete:
            asyncio.create_task(_schedule_delete(m, settings["query_autodelete_seconds"]))


# ── Several titles match: let the user pick ─────────────────────────────────
# "bahubali" is not one movie — Baahubali, The Beginning, The Conclusion, The Epic…
# Instead of guessing one (or listing 66 files of mixed titles) the bot shows one
# button per title (spelling variants merged; remakes split by year) and then the
# files of the one the user taps.
_TITLE_CACHE: dict = {}
_TITLES_PER_PAGE = 8
_FILLER_WORDS = frozenset({"movie", "movies", "film", "films", "download", "full", "hd"})


def _strip_filler(text: str) -> str:
    """"pushpa 2 full movie download" -> "pushpa 2". Only tried when nothing was found."""
    return " ".join(w for w in text.split() if re.sub(r"\W", "", w).lower() not in _FILLER_WORDS)


def _has_tags(query: str) -> bool:
    return any(v is not None for v in parse_query(query)[1].values())


def _title_cache_put(key: str, buttons: list, query: str):
    if len(_TITLE_CACHE) >= _CACHE_MAX_ENTRIES:
        oldest = min(_TITLE_CACHE, key=lambda k: _TITLE_CACHE[k]["time"])
        _TITLE_CACHE.pop(oldest, None)
    _TITLE_CACHE[key] = {"buttons": buttons, "query": query, "time": time.time()}


def _title_cache_get(key: str) -> dict | None:
    entry = _TITLE_CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["time"] > _CACHE_TTL:
        _TITLE_CACHE.pop(key, None)
        return None
    return entry


def _title_menu(key: str, entry: dict, offset: int, header: str):
    buttons = entry["buttons"]
    offset = max(0, min(offset, max(0, len(buttons) - 1)))
    rows = []
    for i in range(offset, min(offset + _TITLES_PER_PAGE, len(buttons))):
        label = "🎬 " + button_label(buttons[i])
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"tt#{key}#{i}")])
    if len(buttons) > _TITLES_PER_PAGE:
        pages = math.ceil(len(buttons) / _TITLES_PER_PAGE)
        nav = []
        if offset > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"tp#{key}#{max(0, offset - _TITLES_PER_PAGE)}"))
        nav.append(InlineKeyboardButton(f"📄 {offset // _TITLES_PER_PAGE + 1}/{pages}", callback_data="noop"))
        if offset + _TITLES_PER_PAGE < len(buttons):
            nav.append(InlineKeyboardButton("➡️", callback_data=f"tp#{key}#{offset + _TITLES_PER_PAGE}"))
        rows.append(nav)
    return header, InlineKeyboardMarkup(rows)


async def _title_choice(query: str):
    """For a search the bot already found files for: does the query ALSO match other
    titles? Returns None (just show the files) or
      {"mode": "files",   ...}  the exact title holds half or more of all matching
                                files -> show its files + a "More titles" button
      {"mode": "buttons", ...}  otherwise -> title buttons, exact title first"""
    buttons = await buttons_for(query)
    if len({tuple(b["members"]) for b in buttons}) <= 1:
        return None
    exact = sum(b["n"] for b in buttons if b["tier"] == 0)
    total = sum(b["n"] for b in buttons)
    return {"mode": "files" if exact and exact * 2 >= total else "buttons", "buttons": buttons}


async def _deliver_title_menu(status, query: str, buttons: list, settings: dict):
    key = _cache_key("titles:" + query)
    _title_cache_put(key, buttons, query)
    count = len({tuple(b["members"]) for b in buttons})
    text, markup = _title_menu(key, _TITLE_CACHE[key], 0,
                               TITLE_PICK_TXT.format(query=html.escape(query), count=count))
    if settings["query_autodelete_enabled"]:
        text += QUERY_AUTODELETE_NOTE.format(duration=format_duration(settings["query_autodelete_seconds"]))
    status = await _status_update(status, text, markup)
    if settings["query_autodelete_enabled"]:
        asyncio.create_task(_schedule_delete(status, settings["query_autodelete_seconds"]))


@Client.on_callback_query(filters.regex(r"^tt#"))
async def title_clicked(_, query):
    _, key, idx = query.data.split("#")
    entry = _title_cache_get(key)
    await query.answer()
    if not entry or int(idx) >= len(entry["buttons"]):
        await _expired(query.message)
        return
    btn = entry["buttons"][int(idx)]
    _SUGGESTION_TRACKER.pop(query.message.id, None)
    results = await files_for_button(btn)
    if not results:
        await query.message.edit_text(
            SUGGESTION_NOT_FOUND_TXT.format(title=html.escape(btn["title"])) + _remaining_note(query.message),
            reply_markup=InlineKeyboardMarkup(_action_rows(btn["title"], query.from_user.id)),
        )
        return
    label = btn["title"] + (f" ({btn['year_label']})" if btn["years"] is not None else "")
    await _deliver_results(query.message, label, results)
    asyncio.create_task(record_search(btn["title"]))
    asyncio.create_task(_schedule_delete(query.message, 0))


@Client.on_callback_query(filters.regex(r"^tp#"))
async def title_page(_, query):
    _, key, offset = query.data.split("#")
    entry = _title_cache_get(key)
    await query.answer()
    if not entry:
        await _expired(query.message)
        return
    count = len({tuple(b["members"]) for b in entry["buttons"]})
    header = TITLE_PICK_TXT.format(query=html.escape(entry["query"]), count=count)
    text, markup = _title_menu(key, entry, int(offset), header)
    await _push(query.message, text + _remaining_note(query.message), markup)


@Client.on_callback_query(filters.regex(r"^mt#"))
async def more_titles(_, query):
    _, key = query.data.split("#")
    entry = _title_cache_get(key)
    await query.answer()
    if not entry:
        await _expired(query.message)
        return
    text, markup = _title_menu(key, entry, 0, TITLE_MORE_HEADER_TXT.format(query=html.escape(entry["query"])))
    sent = await query.message.reply_text(text, reply_markup=markup)
    settings = await get_settings()
    if settings["query_autodelete_enabled"]:
        asyncio.create_task(_schedule_delete(sent, settings["query_autodelete_seconds"]))


@Client.on_callback_query(filters.regex(r"^alt#"))
async def other_matches(_, query):
    _, key = query.data.split("#")
    titles = _suggestion_cache_get(key)
    await query.answer()
    if not titles:
        await _expired(query.message)
        return
    original = _SUGGESTION_CACHE.get(key, {}).get("query", "")
    sent = await query.message.reply_text(
        ALT_HEADER_TXT.format(query=html.escape(original)), reply_markup=_suggestion_keyboard(key, titles),
    )
    settings = await get_settings()
    if settings["query_autodelete_enabled"]:
        asyncio.create_task(_schedule_delete(sent, settings["query_autodelete_seconds"]))


# ── "Title exists, but not that season / year / language / quality" ─────────
# "loki s05" or "jawan 2024": the title is in the database, only the extra tag
# matches nothing. Instead of "no results" the bot says which tag is missing and
# shows what exists — a season menu for series, a note + all files otherwise.
_RELAX_CACHE: dict = {}


def _relax_cache_put(key: str, title: str, results: list):
    if len(_RELAX_CACHE) >= _CACHE_MAX_ENTRIES:
        oldest = min(_RELAX_CACHE, key=lambda k: _RELAX_CACHE[k]["time"])
        _RELAX_CACHE.pop(oldest, None)
    _RELAX_CACHE[key] = {"title": title, "all": results, "time": time.time()}


def _relax_cache_get(key: str) -> dict | None:
    entry = _RELAX_CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["time"] > _CACHE_TTL:
        _RELAX_CACHE.pop(key, None)
        return None
    return entry


def _fmt_tag(name: str, value) -> str:
    if name == "season":
        return f"Season {int(value)}"
    if name == "episode":
        return f"Episode {int(value)}"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _tags_label(tags: dict) -> str:
    return " ".join(_fmt_tag(k, v) for k, v in tags.items())


def _relaxed_note(relaxed: dict) -> str:
    """'⚠️ 2024 isn't available — showing all files.\n📅 Available: 2023'"""
    tags, dropped, meta = relaxed["tags"], relaxed["dropped"], relaxed["meta"]
    missing = ", ".join(_fmt_tag(k, tags[k]) for k in dropped)
    kept = {k: v for k, v in tags.items() if k not in dropped}
    scope = f"showing {_tags_label(kept)} files" if kept else "showing all files"
    note = RELAXED_NOTE_TXT.format(missing=html.escape(missing), scope=html.escape(scope))
    lines = []
    if "season" in dropped and meta["seasons"]:
        lines.append(("Seasons", ", ".join(str(n) for n in meta["seasons"])))
    if "episode" in dropped:
        eps = extract_meta(relaxed["results"])["episodes"]
        if eps:
            lines.append(("Episodes", ", ".join(str(n) for n in eps)))
    if "year" in dropped and meta["years"]:
        lines.append(("Years", ", ".join(meta["years"])))
    if "language" in dropped and meta["languages"]:
        lines.append(("Languages", ", ".join(meta["languages"])))
    if ({"quality", "resolution", "source"} & set(dropped)) and meta["qualities"]:
        lines.append(("Qualities", ", ".join(label for _code, label in meta["qualities"])))
    for label, values in lines:
        note += RELAXED_AVAILABLE_TXT.format(label=label, values=html.escape(values))
    return note


async def _deliver_relaxed(message, status, query: str, relaxed: dict, settings: dict):
    title, dropped, meta = relaxed["title"], relaxed["dropped"], relaxed["meta"]

    # Series where the requested SEASON doesn't exist: a menu of the seasons
    # that do (tap one to open it).
    if "season" in dropped and meta["seasons"]:
        key = _cache_key("seasons:" + title + ":" + query)
        _relax_cache_put(key, title, relaxed["all"])
        buttons = [InlineKeyboardButton(SEASON_BTN.format(n=n), callback_data=f"sfb#{key}#{n}")
                   for n in meta["seasons"]]
        rows = _chunk(buttons, 3)
        rows.append([InlineKeyboardButton(SEASON_ALL_BTN, callback_data=f"sfb#{key}#0")])
        text = SEASON_MENU_TXT.format(
            title=html.escape(title),
            missing=html.escape(_fmt_tag("season", relaxed["tags"]["season"])),
            seasons=", ".join(str(n) for n in meta["seasons"]),
        )
        if settings["query_autodelete_enabled"]:
            text += QUERY_AUTODELETE_NOTE.format(duration=format_duration(settings["query_autodelete_seconds"]))
        status = await _status_update(status, text, InlineKeyboardMarkup(rows))
        if settings["query_autodelete_enabled"]:
            asyncio.create_task(_schedule_delete(status, settings["query_autodelete_seconds"]))
        return

    # Everything else (movie year, language, quality, a missing episode…):
    # the normal results page for what DOES exist, with a note on top.
    kept = {k: v for k, v in relaxed["tags"].items() if k not in dropped}
    resolved = f"{title} {_tags_label(kept)}".strip()
    await _deliver_results(message, resolved, relaxed["results"], note=_relaxed_note(relaxed))
    asyncio.create_task(record_search(title_of(relaxed["results"], title)))
    asyncio.create_task(_schedule_delete(status, 0))


@Client.on_callback_query(filters.regex(r"^sfb#"))
async def season_fallback_clicked(_, query):
    _, key, n = query.data.split("#")
    await query.answer()
    entry = _relax_cache_get(key)
    if not entry:
        await _expired(query.message)
        return
    season = int(n)
    results = entry["all"] if season == 0 else apply_filters(entry["all"], {"season": season})
    if not results:
        await _expired(query.message)
        return
    resolved = entry["title"] if season == 0 else f"{entry['title']} Season {season}"
    await _deliver_results(query.message, resolved, results)
    asyncio.create_task(_schedule_delete(query.message, 0))


@Client.on_message(search_filter)
async def handle_search(bot, message):
    logger.info("Search triggered by user %s: %s", message.from_user.id if message.from_user else "unknown", message.text[:50])
    query = message.text.strip()
    if not query:
        return

    settings = await get_settings()

    # PM Filter switch — only affects the bot's DM. Group search stays always on.
    if message.chat.type == enums.ChatType.PRIVATE and not settings["pm_filter_enabled"]:
        mention = message.from_user.mention if message.from_user else "there"
        markup = (
            InlineKeyboardMarkup([[InlineKeyboardButton(PM_SEARCH_OFF_BTN, url=MOVIE_GROUP_LINK)]])
            if MOVIE_GROUP_LINK else None
        )
        await message.reply_text(
            PM_SEARCH_OFF_TXT.format(mention=mention), reply_markup=markup, quote=True,
        )
        return

    if not settings["autofilter_enabled"]:
        await message.reply_text(MAINTENANCE_TXT, quote=True)
        return

    # Admin-defined filter words ("movie", "full movie"…) are dropped from the
    # query — but only if the words EXACTLY as typed find nothing. A real title
    # can contain one: with "de" as a filter word, "Kankaan de ohle" would
    # otherwise be searched as "Kankaan ohle" and never found.
    typed = query
    _uid = message.from_user.id if message.from_user else None
    _private = message.chat.type == enums.ChatType.PRIVATE

    def _log(outcome: str, shown: int = 0, title: str = ""):
        # Save the search (what was typed + what the bot did) for the admin export.
        if settings["query_log_enabled"]:
            asyncio.create_task(log_query(_uid, typed, outcome, shown, title, _private))

    # What to try, in order: the words exactly as typed; then without the admin's
    # filter words; then also without filler words (movie, film, download…).
    variants = [typed]
    emptied = False   # the filter words would leave nothing of the query
    if settings["filter_words"]:
        filtered = apply_filter_words(typed, settings["filter_words"])
        if filtered != typed:
            if filtered:
                variants.append(filtered)
            else:
                emptied = True
    bare = _strip_filler(variants[-1])
    if bare and bare.lower() != variants[-1].lower() and bare not in variants:
        variants.append(bare)

    # The "Searching..." status message is sent concurrently with the real
    # Stage 1 work below, not before it — so showing search progress never
    # adds latency to the common case where Stage 1 already finds it.
    status_task = asyncio.create_task(
        message.reply_text(STATUS_STAGE1_TXT.format(query=html.escape(typed)), quote=True)
    )
    results, query = [], variants[-1]
    for candidate in variants:
        results = await search_files(candidate)
        if results:
            query = candidate
            break
    status = await status_task
    # A query that is only language and/or year (e.g. "Hindi 2024") has no
    # title of its own — no trending entry for it (its poster still comes
    # from the result files, like every other search).
    lang_year_only = is_language_year_query(query)

    if not results and emptied:
        asyncio.create_task(_schedule_delete(status, 0))
        await message.reply_text(EMPTY_QUERY_TXT, quote=True)
        return

    if results:
        # Stage 1 already found it — the common, fast path. If the words also match
        # OTHER titles ("bahubali": The Beginning, The Conclusion, The Epic…), the
        # user is asked which one — unless the exact title already holds half or more
        # of the matching files, then its files come first with a "More titles" button.
        choice = None
        if not lang_year_only and not _has_tags(query):
            choice = await _title_choice(query)
        if choice and choice["mode"] == "buttons":
            _log("titles", len(choice["buttons"]))
            await _deliver_title_menu(status, query, choice["buttons"], settings)
            return
        extra = None
        if choice:
            more = [b for b in choice["buttons"] if b["tier"] != 0]
            if more:
                mkey = _cache_key("more:" + query)
                _title_cache_put(mkey, more, query)
                extra = [[InlineKeyboardButton(TITLE_MORE_BTN.format(n=len(more)), callback_data=f"mt#{mkey}")]]
        await _deliver_results(message, query, results, extra_rows=extra)
        _log("found", len(results), title_of(results, query))
        if not lang_year_only:
            asyncio.create_task(record_search(title_of(results, query)))
            asyncio.create_task(note_success(message.from_user.id if message.from_user else None, results))
        asyncio.create_task(_schedule_delete(status, 0))
        return

    # The title exists but the extra tag (season / year / language / quality)
    # matches nothing — show what DOES exist instead of "no results".
    if not lang_year_only:
        relaxed = await relaxed_search(query)
        if relaxed:
            _log("relaxed", len(relaxed["results"]), relaxed["title"])
            await _deliver_relaxed(message, status, query, relaxed, settings)
            return

    # Phrase search — titles that ARE, START WITH or CONTAIN the words, spelling-
    # proof (Bahubali = Baahubali). One title -> its files; several -> title buttons.
    if not lang_year_only:
        buttons, used = [], query
        for candidate in variants:
            buttons = await buttons_for(candidate)
            if buttons:
                used = candidate
                break
        if buttons:
            if len(buttons) == 1:
                found = await files_for_button(buttons[0])
                if found:
                    label = buttons[0]["title"] + (f" ({buttons[0]['year_label']})" if buttons[0]["years"] is not None else "")
                    await _deliver_results(message, label, found)
                    _log("found", len(found), label)
                    asyncio.create_task(record_search(buttons[0]["title"]))
                    asyncio.create_task(_schedule_delete(status, 0))
                    return
            else:
                _log("titles", len(buttons))
                await _deliver_title_menu(status, used, buttons, settings)
                return

    # Stage 2 — fuzzy match against EVERY title in your database (titleindex.py).
    # Free, in-memory, tens of milliseconds. Catches ordinary typos without ever
    # cross-matching a different-but-similar-looking title (word count must
    # match and every word must score high individually).
    status = await _status_update(status, STATUS_STAGE2_TXT)

    hit, used = None, query
    for candidate in variants:
        hit = await fuzzy_correct(candidate)
        if hit:
            used = candidate
            break
    if hit:
        resolved_query, results = hit
        # It always shows the closest title, but when other titles are almost as close
        # a button lets the user look at them instead.
        extra = None
        alternatives = await fuzzy_alternatives(used, resolved_query)
        if alternatives:
            akey = _cache_key("alt:" + typed)
            _suggestion_cache_put(akey, alternatives, typed)
            extra = [[InlineKeyboardButton(ALT_BTN, callback_data=f"alt#{akey}")]]
        await _deliver_results(message, resolved_query, results, original_query=typed, extra_rows=extra)
        _log("fuzzy", len(results), resolved_query)
        asyncio.create_task(record_search(title_of(results, resolved_query)))
        asyncio.create_task(_schedule_delete(status, 0))
        return

    # Stage 3 — TMDB/OMDb are searched first (every result they return is
    # a real, catalogued title); Groq + Gemini only run as a last resort if
    # both come back empty. Either way this only ever produces a list of
    # candidate titles to show as buttons — nothing is searched in your
    # database, and nothing is shown as a result, until the user actually
    # taps one.
    # First look for titles in OUR database that start with the query
    # ("pushpa" -> "Pushpa The Rise", "Pushpa 2 The Rule"): they are real,
    # they have files, and it needs no outside API. Only when there are none
    # does the bot ask TMDB/OMDb/AI for suggestions.
    related = []
    for candidate in variants:
        related = await related_titles(candidate)
        if related:
            break
    if related:
        suggestions = related
    else:
        status = await _status_update(status, STATUS_STAGE3_TXT)
        suggestions = []
        for candidate in variants:
            suggestions = await suggest_titles(candidate)
            if suggestions:
                break
    user_id = message.from_user.id if message.from_user else None
    asyncio.create_task(note_miss(user_id, typed))  # AI learning: what users wanted but didn't get
    _log("related" if related else "suggested" if suggestions else "none")

    rows = []
    if suggestions:
        key = _cache_key(query)
        _suggestion_cache_put(key, suggestions, query)
        rows.extend(_suggestion_keyboard(key, suggestions).inline_keyboard)
    # Google search + Request to Admin under BOTH the suggestions message and
    # the plain "not found" message.
    rows.extend(_action_rows(query, user_id))
    markup = InlineKeyboardMarkup(rows)

    text = (
        (RELATED_HEADER_TXT if related else SUGGESTIONS_HEADER_TXT).format(query=html.escape(query)) if suggestions
        else NOT_FOUND_TXT.format(query=html.escape(query))
    )
    autodelete = settings["query_autodelete_enabled"]
    if autodelete:
        text += QUERY_AUTODELETE_NOTE.format(duration=format_duration(settings["query_autodelete_seconds"]))
    status = await _status_update(status, text, markup)
    if autodelete:
        # Same timer as the result messages: the "not found" / suggestions / Google /
        # Request-to-admin message disappears on its own. If the user taps a
        # suggestion or Request, this same message is edited, and still vanishes.
        asyncio.create_task(_schedule_delete(status, settings["query_autodelete_seconds"]))

    # Track for auto-request timeout
    if REQUEST_CHANNEL and user_id and user_id not in ADMINS:
        _SUGGESTION_TRACKER[status.id] = {
            "clicked": False,
            "query": query,
            "user_id": user_id
        }
        asyncio.create_task(_schedule_auto_request(bot, status, query, user_id))
    
    return


# ══════════════════════════════════════════════════════════════════════════════
# Callbacks — pagination, filter menus, filter selection, home reset
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^noop$"))
async def noop(_, query):
    await query.answer()


@Client.on_callback_query(filters.regex(r"^pg#"))
async def paginate(_, query):
    _, key, offset = query.data.split("#")
    entry = _cache_get(key)
    await query.answer()
    if not entry:
        await _expired(query.message)
        return
    text, markup = _render(key, entry, int(offset))
    await _push(query.message, text, markup)


@Client.on_callback_query(filters.regex(r"^fm#"))
async def open_filter_menu(_, query):
    _, key, ftype, offset = query.data.split("#")
    entry = _cache_get(key)
    await query.answer()
    if not entry:
        await _expired(query.message)
        return
    text, markup = _filter_menu(key, ftype, entry, int(offset))
    await _push(query.message, text, markup)


@Client.on_callback_query(filters.regex(r"^fv#"))
async def set_filter_value(_, query):
    _, key, ftype, value, offset = query.data.split("#")
    entry = _cache_get(key)
    await query.answer()
    if not entry:
        await _expired(query.message)
        return
    entry["filters"][ftype] = None if value == "_any_" else value
    text, markup = _render(key, entry, offset=0)
    await _push(query.message, text, markup)


@Client.on_callback_query(filters.regex(r"^rr#"))
async def return_to_results(_, query):
    _, key, offset = query.data.split("#")
    entry = _cache_get(key)
    await query.answer()
    if not entry:
        await _expired(query.message)
        return
    text, markup = _render(key, entry, int(offset))
    await _push(query.message, text, markup)


@Client.on_callback_query(filters.regex(r"^home#"))
async def reset_home(_, query):
    _, key = query.data.split("#")
    entry = _cache_get(key)
    await query.answer()
    if not entry:
        await _expired(query.message)
        return
    entry["filters"] = dict(_EMPTY_FILTERS)
    text, markup = _render(key, entry, offset=0)
    await _push(query.message, text, markup)


@Client.on_callback_query(filters.regex(r"^sug#"))
async def suggestion_clicked(_, query):
    _, key, idx = query.data.split("#")
    titles = _suggestion_cache_get(key)
    await query.answer()
    if not titles or int(idx) >= len(titles):
        await _expired(query.message)
        return

    # Mark as clicked to prevent auto-request
    _SUGGESTION_TRACKER.pop(query.message.id, None)

    title = titles[int(idx)]
    results = await search_files(title)
    if not results:
        await query.message.edit_text(
            SUGGESTION_NOT_FOUND_TXT.format(title=html.escape(title)) + _remaining_note(query.message),
            reply_markup=InlineKeyboardMarkup(_action_rows(title, query.from_user.id)),
        )
        return

    await _deliver_results(query.message, title, results)
    original = _SUGGESTION_CACHE.get(key, {}).get("query", "")
    if original:
        asyncio.create_task(note_tap(query.from_user.id, original, title))  # AI learning
    asyncio.create_task(_schedule_delete(query.message, 0))


@Client.on_callback_query(filters.regex(r"^req#"))
async def request_button_clicked(bot, query):
    """'Request to Admin' button — sends the request to the request channel
    (same thing /req does) and confirms to the user."""
    try:
        _, key, user_id = query.data.split("#")
        user_id = int(user_id)
    except ValueError:
        await query.answer("⚠️ Invalid request button.", show_alert=True)
        return

    # Only the user the button was made for can trigger their own request
    if query.from_user.id != user_id:
        await query.answer("⚠️ This is not your request!", show_alert=True)
        return

    if not REQUEST_CHANNEL:
        await query.answer(REQUEST_NOT_CONFIGURED_TXT, show_alert=True)
        return

    search_query = _request_cache_get(key)
    if not search_query:
        await query.answer("⏳ This button expired — please search again.", show_alert=True)
        return

    await query.answer("📮 Sending request to admin...")

    # Import here to avoid circular dependency
    from plugins.request import send_request

    username = query.from_user.username or query.from_user.first_name
    sent = await send_request(
        bot,
        user_id=user_id,
        query=search_query,
        username=username,
        origin_message=query.message.reply_to_message or query.message,
    )

    if sent:
        # The request is in — the auto "file not found" notice is no longer needed.
        _SUGGESTION_TRACKER.pop(query.message.id, None)
        await query.message.edit_text(
            REQUEST_SENT_TXT.format(query=html.escape(search_query)) + _remaining_note(query.message),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ View Your Request ✨", url=sent.link)]
            ])
        )
    else:
        # Keep the message and its buttons so the user can simply tap again.
        await query.answer("❌ Couldn't send your request right now. Please try again.", show_alert=True)
