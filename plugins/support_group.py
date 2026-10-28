"""
Support-group helper.

In the support group (see SUPPORT_GROUP_IDS / SUPPORT_GROUP_USERNAMES in config.py) users
type movie / series names in any pattern ("jawan hindi hai kya", "pls send pushpa 2 link").
The normal autofilter is OFF there. Instead:

  1. AI (Groq, then Gemini) reads the message and extracts the title, year, season and a
     confidence. Chit-chat ("hello", "bot not working") is ignored silently.
  2. The title is looked up in the database (aliases, typo fixer and phrase search included).
  3. SURE + FOUND  -> one message in the group: poster at the top, file name, genres,
     quality, resolution, audio, rating (same look as the movie update post, details in a
     quote box) and a  [Get Files]  button that opens the bot DM and shows all files.
  4. NOT SURE / NOT FOUND / SEASON MISSING / SEVERAL TITLES -> the bot stays silent in the
     group and posts a report in the #FILE_NOT_FOUND channel with the buttons
        [Already uploaded] [Now uploaded] [Spelling mistake] [Custom message] [Ignore]
     The first admin who presses one decides what the user is told; the bot then posts that
     answer in the support group as a reply to the user's message.

Notes
  * The channel is private. An admin's typed reply (spelling / custom text) comes from the
    channel itself, so what protects it is that only the channel's admins can post there.
    Button presses identify the person and are checked against ADMINS.
  * Reports live in Mongo (collection support_asks) so the buttons keep working after a restart.
"""
import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from collections import defaultdict

import aiohttp
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, ReturnDocument
from pyrogram import Client, filters, enums
from pyrogram.errors import RPCError
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import (
    ADMINS, NOT_FOUND_FILE_CHANNEL, MOVIE_GROUP_LINK,
    GROQ_API_KEY, GROQ_MODEL, GEMINI_API_KEY, GEMINI_MODEL, AI_FETCH_TIMEOUT,
    is_support_chat,
)
from database.client import db
from database.filters_db import search_files
from linkcheck import has_link
from movie_metadata import get_movie_detailsx
from plugins.movie_update import extract_media_info, _plain, _format_episodes, _res_sort_key
from poster import fetch_poster_for_results, _pick_title_from_results
from spellcheck import fuzzy_correct
from strings import MANUAL_UPDATE_NOTIFY_TXT
from titleindex import buttons_for, files_for_button
from utils import temp

logger = logging.getLogger(__name__)

HTML = enums.ParseMode.HTML
asks = db["support_asks"]

CONF_MIN = 70            # AI confidence (0-100) needed to answer without asking an admin
MAX_TEXT = 300           # longer messages are not movie requests
ASK_TTL_DAYS = 30
CAPTION_LIMIT = 1024

_BG: set = set()
_indexes_ready = False
_LAST_USER: dict = {}    # user_id -> time of the last message looked at
_SEEN_TEXT: dict = {}    # (chat, user, text) -> time, so a repeated message is not handled twice
_AI_SEM = asyncio.Semaphore(4)


# ══════════════════════════════════════════════════════════════════════════════
# AI: what movie / series is this message about?
# ══════════════════════════════════════════════════════════════════════════════

_AI_PROMPT = (
    "You read messages written by users in the SUPPORT group of a Telegram movie bot. People write "
    "in English, Hindi, Hinglish or Punjabi (any script), with spelling mistakes and in any pattern, "
    "for example \"jawan hindi hai kya\", \"bhai pushpa 2 ka link do\", \"kal wali movie upload hui?\". "
    "Decide whether the message asks for, mentions or searches ONE specific movie, web series, anime or show, "
    "and extract it.\n"
    "Reply with JSON only, exactly this shape:\n"
    "{\"is_title\": true or false, \"title\": \"...\", \"year\": \"\", \"season\": null, "
    "\"language\": \"\", \"confidence\": 0-100}\n"
    "Rules:\n"
    "- title: the real title spelled correctly in English letters if you know it, otherwise as typed. "
    "Remove filler words (movie, film, series, hai, kya, send, link, please, download, full, hd, chahiye, do).\n"
    "- season: a number only if the user asked for a season (s02, season 2), else null.\n"
    "- year: 4 digits only if the user wrote it, else empty.\n"
    "- confidence: how sure you are that `title` is exactly the title the user wants. "
    "If the message could be several titles, or the title is unclear, give 50 or less.\n"
    "- is_title is false for greetings, thanks, complaints, questions about the bot, premium, payments, "
    "general chat, or when no title is named. Then title is empty.\n"
    "- If several different titles are named, use the first one and give confidence 50 or less.\n\n"
    "Message: "
)


def _parse_ai(text: str):
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()[:100]
    year = str(data.get("year") or "").strip()
    if not re.fullmatch(r"(19|20)\d{2}", year):
        year = ""
    season = data.get("season")
    try:
        season = int(season) if season not in (None, "", "null") else None
    except (TypeError, ValueError):
        season = None
    if season is not None and not 1 <= season <= 60:
        season = None
    try:
        confidence = max(0, min(100, int(float(data.get("confidence", 0)))))
    except (TypeError, ValueError):
        confidence = 0
    is_title = data.get("is_title") in (True, "true", "True", 1) and bool(title)
    return {"is_title": is_title, "title": title if is_title else "", "year": year, "season": season,
            "language": str(data.get("language") or "").strip()[:30], "confidence": confidence}


async def _ask_groq(prompt: str):
    if not GROQ_API_KEY:
        return None
    payload = {
        "model": GROQ_MODEL, "temperature": 0, "max_tokens": 160,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        timeout = aiohttp.ClientTimeout(total=AI_FETCH_TIMEOUT + 2)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://api.groq.com/openai/v1/chat/completions", json=payload,
                                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"}) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        return _parse_ai(data["choices"][0]["message"]["content"])
    except Exception:
        logger.debug("Groq title detection failed", exc_info=True)
        return None


async def _ask_gemini(prompt: str):
    if not GEMINI_API_KEY:
        return None
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0, "maxOutputTokens": 200}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    try:
        timeout = aiohttp.ClientTimeout(total=AI_FETCH_TIMEOUT + 2)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers={
                    "x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        return _parse_ai(data["candidates"][0]["content"]["parts"][0]["text"])
    except Exception:
        logger.debug("Gemini title detection failed", exc_info=True)
        return None


async def detect_title(text: str):
    """-> dict (see _parse_ai) or None when no AI could answer."""
    prompt = _AI_PROMPT + json.dumps(text, ensure_ascii=False)
    async with _AI_SEM:
        result = await _ask_groq(prompt)
        if result is None:
            result = await _ask_gemini(prompt)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Database lookup
# ══════════════════════════════════════════════════════════════════════════════

def _dedupe(files: list) -> list:
    seen, out = set(), []
    for f in files:
        fid = f.get("_id")
        if fid in seen:
            continue
        seen.add(fid)
        out.append(f)
    return out


async def _exact_title_files(title: str, results: list):
    """The search found files. If the words ALSO match other titles and none of them is the
    exact title, we can't be sure which one the user means -> None (ask an admin)."""
    buttons = await buttons_for(title)
    if len({tuple(b["members"]) for b in buttons}) <= 1:
        return results
    exact = [b for b in buttons if b["tier"] == 0]
    if not exact:
        return None
    files = []
    for b in exact:
        files += await files_for_button(b)
    return _dedupe(files) or results


async def resolve(title: str, year: str = "", season=None) -> dict:
    """-> {"state": found | partial | ambiguous | none, "results": [...], "via": "..."}
    partial = the title is there but the asked season is not."""
    queries = []
    if season:
        queries.append(f"{title} s{int(season):02d}")
    if year:
        queries.append(f"{title} {year}")
    queries.append(title)

    results, via = [], "exact"
    for q in queries:
        results = await search_files(q)
        if results:
            break

    if results:
        narrowed = await _exact_title_files(title, results)
        if narrowed is None:
            return {"state": "ambiguous", "results": results, "via": via}
        results = narrowed
    else:
        buttons = await buttons_for(title)
        groups = {tuple(b["members"]) for b in buttons}
        exact = [b for b in buttons if b["tier"] == 0]
        pick = exact or (buttons if len(groups) == 1 else [])
        if pick:
            files = []
            for b in pick:
                files += await files_for_button(b)
            results, via = _dedupe(files), "phrase"
        elif buttons:
            return {"state": "ambiguous", "results": [], "via": "phrase"}
        else:
            hit = await fuzzy_correct(title)
            if hit:
                results, via = hit[1], "fuzzy"

    if not results:
        return {"state": "none", "results": [], "via": via}

    if season:
        has_season = any(
            extract_media_info(f.get("file_name", ""), _plain(f.get("caption", "") or ""))["season"] == int(season)
            for f in results
        )
        if not has_season:
            return {"state": "partial", "results": results, "via": via}
    return {"state": "found", "results": results, "via": via}


# ══════════════════════════════════════════════════════════════════════════════
# The "already uploaded" card (same look as the movie update message)
# ══════════════════════════════════════════════════════════════════════════════

def _mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{html.escape(name or str(user_id))}</a>'


def _main_group_markup():
    if not MOVIE_GROUP_LINK:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Open Main Group", url=MOVIE_GROUP_LINK)]])


def _getfiles_query(title: str) -> str:
    """Words for the /start getfile-<words> deep link (payload cap is 64 chars)."""
    words = re.sub(r"[^A-Za-z0-9_\s]", " ", title).split()
    out = ""
    for w in words:
        candidate = f"{out}-{w}" if out else w
        if len(candidate) > 56:
            break
        out = candidate
    return out


async def build_card(results: list, fallback_title: str, header: str):
    """-> (caption text, poster dict or None, InlineKeyboardMarkup)"""
    dtitle, dyear, _hint = _pick_title_from_results(results)
    title = dtitle or fallback_title

    infos = [extract_media_info(f.get("file_name", ""), _plain(f.get("caption", "") or "")) for f in results]
    qualities, resolutions, languages, otts = set(), set(), set(), set()
    episodes = defaultdict(set)
    is_series = False
    for info in infos:
        if info["quality"] != "N/A":
            qualities.update(x.strip() for x in info["quality"].split(",") if x.strip())
        if info["resolution"] != "N/A":
            resolutions.update(x.strip() for x in info["resolution"].split(",") if x.strip())
        if info["language"] != "N/A":
            languages.update(x.strip() for x in info["language"].split(",") if x.strip())
        if info["ott_platform"] != "N/A":
            otts.update(p.strip() for p in info["ott_platform"].split("|") if p.strip())
        if info["season"] is not None:
            is_series = True
            if info["episode"] is not None:
                episodes[str(info["season"])].add(str(info["episode"]))

    poster, details = await asyncio.gather(
        fetch_poster_for_results(results),
        get_movie_detailsx(title, year=dyear or None, prefer_tv=is_series),
        return_exceptions=True,
    )
    if isinstance(poster, Exception):
        poster = None
    if isinstance(details, Exception) or not isinstance(details, dict) or details.get("error"):
        details = {}

    raw_genres = details.get("genres", "") or ""
    if isinstance(raw_genres, list):
        genres = ", ".join(str(g) for g in raw_genres if g) or "N/A"
    else:
        genres = ", ".join(g.strip() for g in str(raw_genres).split(",") if g.strip()) or "N/A"
    try:
        rating = f"{float(details.get('rating')):.1f}"
    except (TypeError, ValueError):
        rating = "N/A"

    shown = details.get("title") or title
    year = dyear or details.get("year") or ""
    if year and str(year) not in shown:
        shown = f"{shown} ({year})"

    e = html.escape

    def make(epi: str) -> str:
        body = MANUAL_UPDATE_NOTIFY_TXT.format(
            tag="#SERIES" if is_series else "#MOVIE",
            filename=e(str(shown)),
            genres=e(genres),
            ott=e(" | ".join(sorted(otts)) or "N/A"),
            quality=e(", ".join(sorted(qualities)) or "N/A"),
            resolution=e(", ".join(sorted(resolutions, key=_res_sort_key)) or "N/A"),
            language=e(", ".join(sorted(languages)) or "N/A"),
            rating=e(rating),
            episodes=epi,
        )
        return header + body

    text = make(_format_episodes(episodes))
    if len(text) > CAPTION_LIMIT:      # a photo caption is capped; the episode list is the long part
        text = make("")
    if len(text) > CAPTION_LIMIT:
        text = text[:CAPTION_LIMIT - 1]

    query = _getfiles_query(title)
    link = f"https://t.me/{temp.U_NAME}?start=getfile-{query}" if query else f"https://t.me/{temp.U_NAME}"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔍 ɢᴇᴛ ꜰɪʟᴇs", url=link)]])
    return text, poster, markup


async def _send(bot, chat_id: int, reply_to, text: str, markup=None, photo: str | None = None):
    """Send to the support group, as a reply when possible. Photo first, plain text as fallback."""
    for rt in ((reply_to, None) if reply_to else (None,)):
        if photo:
            try:
                return await bot.send_photo(chat_id, photo, caption=text, reply_to_message_id=rt,
                                            reply_markup=markup, parse_mode=HTML)
            except Exception:
                logger.debug("Poster send failed, sending text", exc_info=True)
        try:
            return await bot.send_message(chat_id, text, reply_to_message_id=rt, reply_markup=markup,
                                          parse_mode=HTML, disable_web_page_preview=True)
        except RPCError:
            logger.debug("Send to support group failed (reply_to=%s)", rt, exc_info=True)
    return None


async def _send_card(bot, chat_id: int, reply_to, user_id: int, name: str, results: list,
                     title: str, note: str = ""):
    header = f"{_mention(user_id, name)} 👋\n<b>✅ ᴀʟʀᴇᴀᴅʏ ᴜᴘʟᴏᴀᴅᴇᴅ</b>{note}\n\n"
    text, poster, markup = await build_card(results, title, header)
    return await _send(bot, chat_id, reply_to, text, markup, poster["url"] if poster else None)


# ══════════════════════════════════════════════════════════════════════════════
# Reports for admins (#FILE_NOT_FOUND channel)
# ══════════════════════════════════════════════════════════════════════════════

OPEN, WAIT_SP, WAIT_CU, DONE, IGNORED = "open", "waiting_sp", "waiting_cu", "done", "ignored"


def _oid(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


async def _ensure_indexes():
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        await asks.create_index([("created", ASCENDING)], expireAfterSeconds=ASK_TTL_DAYS * 86400, name="ask_ttl")
        await asks.create_index([("nf_msg_id", ASCENDING)], name="ask_nf")
        await asks.create_index([("prompt_id", ASCENDING)], name="ask_prompt")
        _indexes_ready = True
    except Exception:
        logger.exception("Could not create support_asks indexes")


async def _transition(ask_id, from_states: list, new_state: str, extra: dict | None = None):
    """Atomic state change; None if somebody else got there first (first admin wins)."""
    oid = _oid(ask_id)
    if not oid:
        return None
    update = {"state": new_state}
    if extra:
        update.update(extra)
    return await asks.find_one_and_update(
        {"_id": oid, "state": {"$in": from_states}}, {"$set": update}, return_document=ReturnDocument.AFTER)


def _open_markup(ask_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Already uploaded", callback_data=f"sga#up#{ask_id}"),
         InlineKeyboardButton("🆕 Now uploaded", callback_data=f"sga#nw#{ask_id}")],
        [InlineKeyboardButton("✏️ Spelling mistake", callback_data=f"sga#sp#{ask_id}"),
         InlineKeyboardButton("💬 Custom message", callback_data=f"sga#cu#{ask_id}")],
        [InlineKeyboardButton("🚫 Ignore", callback_data=f"sga#ig#{ask_id}")],
    ])


def _cancel_markup(ask_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"sga#cn#{ask_id}")]])


async def _edit(bot, doc: dict, text: str, markup=None):
    try:
        await bot.edit_message_text(NOT_FOUND_FILE_CHANNEL, doc["nf_msg_id"], text,
                                    parse_mode=HTML, reply_markup=markup)
    except RPCError as exc:
        logger.debug("Edit of report %s failed: %s", doc.get("nf_msg_id"), exc)


async def _delete_quietly(bot, *msg_ids):
    ids = [m for m in msg_ids if m]
    if ids:
        try:
            await bot.delete_messages(NOT_FOUND_FILE_CHANNEL, ids)
        except RPCError:
            pass


async def _notice(bot, text: str, seconds: int = 8):
    try:
        note = await bot.send_message(NOT_FOUND_FILE_CHANNEL, text, parse_mode=HTML)
    except Exception:
        return

    async def _later():
        await asyncio.sleep(seconds)
        await _delete_quietly(bot, note.id)

    task = asyncio.create_task(_later())
    _BG.add(task)
    task.add_done_callback(_BG.discard)


def _report_text(doc: dict) -> str:
    ai_line = "—"
    if doc.get("title"):
        extra = []
        if doc.get("season"):
            extra.append(f"S{int(doc['season']):02d}")
        if doc.get("year"):
            extra.append(doc["year"])
        ai_line = (f"<b>{html.escape(doc['title'])}</b>" + (f" ({' '.join(extra)})" if extra else "")
                   + f" · {doc.get('confidence', 0)}% sure")
    link = f'\n🔗 <a href="{html.escape(doc["link"], quote=True)}">Open message</a>' if doc.get("link") else ""
    return (
        "<b>#SUPPORT_GROUP</b>\n\n"
        f"👤 User: {_mention(doc['user_id'], doc.get('user_name'))}\n"
        f"🆔 ID: <code>{doc['user_id']}</code>\n"
        f"💬 Message: <code>{html.escape(doc['text'])}</code>\n"
        f"🤖 AI thinks: {ai_line}\n"
        f"📌 Why asking: {html.escape(doc.get('reason', ''))}"
        f"{link}"
    )


async def _create_ask(bot, message, ai, reason: str):
    if not NOT_FOUND_FILE_CHANNEL:
        return
    await _ensure_indexes()
    user = message.from_user
    text = message.text.strip()
    # The same person asking the same thing again while it is still open: one report is enough.
    dup = await asks.find_one({"chat_id": message.chat.id, "user_id": user.id, "text": text,
                               "state": {"$in": [OPEN, WAIT_SP, WAIT_CU]}})
    if dup:
        return
    try:
        link = message.link
    except Exception:
        link = None
    doc = {
        "chat_id": message.chat.id, "msg_id": message.id, "user_id": user.id,
        "user_name": user.first_name or str(user.id), "text": text, "link": link,
        "title": (ai or {}).get("title", ""), "year": (ai or {}).get("year", ""),
        "season": (ai or {}).get("season"), "confidence": (ai or {}).get("confidence", 0),
        "reason": reason, "state": OPEN, "nf_msg_id": None, "prompt_id": None,
        "created": datetime.now(timezone.utc),
    }
    res = await asks.insert_one(doc)
    doc["_id"] = res.inserted_id
    try:
        sent = await bot.send_message(NOT_FOUND_FILE_CHANNEL, _report_text(doc), parse_mode=HTML,
                                      reply_markup=_open_markup(str(res.inserted_id)),
                                      disable_web_page_preview=True)
        await asks.update_one({"_id": res.inserted_id}, {"$set": {"nf_msg_id": sent.id}})
    except Exception:
        logger.exception("Could not post the support-group report")
        await asks.delete_one({"_id": res.inserted_id})


# ── what the user is told ───────────────────────────────────────────────────

async def _relay_uploaded(bot, doc: dict, now: bool):
    """Admin said 'already uploaded' / 'now uploaded'. If the AI's title is confident and the files
    are in the database the full card is sent, otherwise a short text with the main group button."""
    title = doc.get("title") if doc.get("confidence", 0) >= CONF_MIN else ""
    label = "🆕 ɴᴏᴡ ᴜᴘʟᴏᴀᴅᴇᴅ" if now else "✅ ᴀʟʀᴇᴀᴅʏ ᴜᴘʟᴏᴀᴅᴇᴅ"
    if title:
        try:
            loc = await resolve(title, doc.get("year", ""), doc.get("season"))
        except Exception:
            logger.exception("Lookup failed while answering the support group")
            loc = {"state": "none", "results": []}
        if loc["state"] in ("found", "partial") and loc["results"]:
            header = f"{_mention(doc['user_id'], doc.get('user_name'))} 👋\n<b>{label}</b>\n\n"
            text, poster, markup = await build_card(loc["results"], title, header)
            return await _send(bot, doc["chat_id"], doc["msg_id"], text, markup, poster["url"] if poster else None)
    what = f"<b>{html.escape(title)}</b>" if title else "the movie / series you asked for"
    text = (f"{_mention(doc['user_id'], doc.get('user_name'))} 👋\n<b>{label}</b>\n\n"
            f"{what} is available. Please send its <b>name</b> in the main group 👇")
    return await _send(bot, doc["chat_id"], doc["msg_id"], text, _main_group_markup())


async def _relay_spelling(bot, doc: dict, spelling: str):
    name = _mention(doc["user_id"], doc.get("user_name"))
    try:
        loc = await resolve(spelling)
    except Exception:
        loc = {"state": "none", "results": []}
    if loc["state"] == "found" and loc["results"]:
        header = (f"{name} 👋\n<b>✏️ ʏᴏᴜʀ sᴘᴇʟʟɪɴɢ ᴡᴀs ᴡʀᴏɴɢ</b>\n"
                  f"✅ ᴀʟʀᴇᴀᴅʏ ᴜᴘʟᴏᴀᴅᴇᴅ — ᴄᴏʀʀᴇᴄᴛ ɴᴀᴍᴇ: <b>{html.escape(spelling)}</b>\n\n")
        text, poster, markup = await build_card(loc["results"], spelling, header)
        return await _send(bot, doc["chat_id"], doc["msg_id"], text, markup, poster["url"] if poster else None)
    text = (f"{name} 👋\n<b>✏️ ʏᴏᴜʀ sᴘᴇʟʟɪɴɢ ɪs ᴡʀᴏɴɢ</b>\n\n"
            f"Correct name: <b>{html.escape(spelling)}</b>\n"
            "Please send this name in the main group 👇")
    return await _send(bot, doc["chat_id"], doc["msg_id"], text, _main_group_markup())


async def _relay_custom(bot, doc: dict, custom: str):
    text = f"{_mention(doc['user_id'], doc.get('user_name'))} 👋\n\n{html.escape(custom)}"
    return await _send(bot, doc["chat_id"], doc["msg_id"], text)


# ── buttons ─────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^sga#(up|nw|sp|cu|ig|cn)#"))
async def support_buttons(bot, query):
    if query.from_user.id not in ADMINS:
        await query.answer("Admins only.", show_alert=True)
        return

    _, action, ask_id = query.data.split("#", 2)
    handled = "Already handled by another admin."

    if action in ("up", "nw"):
        doc = await _transition(ask_id, [OPEN], DONE, {"result": "uploaded" if action == "up" else "now"})
        if not doc:
            return await query.answer(handled, show_alert=True)
        await query.answer("Sending to the support group…")
        sent = await _relay_uploaded(bot, doc, now=(action == "nw"))
        label = "✅ Already uploaded" if action == "up" else "🆕 Now uploaded"
        tail = "sent to the support group" if sent else "⚠️ could not post in the support group"
        await _edit(bot, doc, f"{_report_text(doc)}\n\n<b>{label}</b> — {tail}.")
        return

    if action in ("sp", "cu"):
        waiting = WAIT_SP if action == "sp" else WAIT_CU
        doc = await _transition(ask_id, [OPEN], waiting)
        if not doc:
            return await query.answer(handled, show_alert=True)
        what = "the <b>correct spelling</b>" if action == "sp" else "your <b>custom message</b>"
        try:
            prompt = await bot.send_message(
                NOT_FOUND_FILE_CHANNEL, f"↩️ Reply to this message with {what}.",
                parse_mode=HTML, reply_to_message_id=doc["nf_msg_id"], reply_markup=_cancel_markup(ask_id))
            await asks.update_one({"_id": doc["_id"]}, {"$set": {"prompt_id": prompt.id}})
        except Exception:
            logger.exception("Could not post the reply prompt")
        await _edit(bot, doc, f"{_report_text(doc)}\n\n⏳ <i>Waiting for an admin's reply…</i>", _cancel_markup(ask_id))
        return await query.answer("Reply to the message in the channel.")

    if action == "cn":
        doc = await _transition(ask_id, [WAIT_SP, WAIT_CU], OPEN)
        if not doc:
            return await query.answer(handled, show_alert=True)
        await _delete_quietly(bot, doc.get("prompt_id"))
        await _edit(bot, doc, _report_text(doc), _open_markup(ask_id))
        return await query.answer("Cancelled.")

    if action == "ig":
        doc = await _transition(ask_id, [OPEN, WAIT_SP, WAIT_CU], IGNORED)
        if not doc:
            return await query.answer(handled, show_alert=True)
        await _delete_quietly(bot, doc.get("prompt_id"))
        await _edit(bot, doc, f"{_report_text(doc)}\n\n🚫 <b>Ignored.</b>")
        return await query.answer("Ignored.")


# ── the admin's reply in the channel (spelling / custom message) ────────────

def _is_nf_channel(_, __, message) -> bool:
    return bool(NOT_FOUND_FILE_CHANNEL) and message.chat is not None and message.chat.id == NOT_FOUND_FILE_CHANNEL


nf_channel_filter = filters.create(_is_nf_channel)


def _is_own_message(message) -> bool:
    """The bot's own posts come back as updates — never treat them as an admin's answer."""
    if message.outgoing or message.reply_markup or getattr(message, "via_bot", None):
        return True
    user = message.from_user
    return bool(user and (user.is_self or user.is_bot))


@Client.on_message(nf_channel_filter & filters.reply & filters.text, group=-2)
async def support_admin_reply(bot, message):
    # Shares its group with the alias-fix and request reply handlers: a reply that is not about
    # a support-group report must be PASSED ON, or the others would never see it.
    if (message.text or "").lstrip().startswith("/") or _is_own_message(message):
        message.continue_propagation()
    rid = message.reply_to_message_id
    doc = await asks.find_one({"state": {"$in": [WAIT_SP, WAIT_CU]},
                               "$or": [{"nf_msg_id": rid}, {"prompt_id": rid}]})
    if not doc:
        message.continue_propagation()

    try:
        typed = (message.text or "").strip()
        was = doc["state"]
        claimed = await _transition(str(doc["_id"]), [was], DONE, {"result": "spelling" if was == WAIT_SP else "custom"})
        if not claimed or not typed:
            return
        if was == WAIT_SP:
            sent = await _relay_spelling(bot, claimed, typed)
            label = f"✏️ Spelling mistake → <code>{html.escape(typed)}</code>"
        else:
            sent = await _relay_custom(bot, claimed, typed)
            label = "💬 Custom message sent"
        await _delete_quietly(bot, message.id, claimed.get("prompt_id"))
        tail = "sent to the support group" if sent else "⚠️ could not post in the support group"
        await _edit(bot, claimed, f"{_report_text(claimed)}\n\n<b>{label}</b> — {tail}.")
        if not sent:
            await _notice(bot, "⚠️ Couldn't post in the support group. Is the bot still an admin there?")
    finally:
        message.stop_propagation()   # we own this reply


# ══════════════════════════════════════════════════════════════════════════════
# The support-group message handler
# ══════════════════════════════════════════════════════════════════════════════

def _support_filter(_, __, message) -> bool:
    if not message.text or message.text.startswith("/") or not is_support_chat(message.chat):
        return False
    user = message.from_user
    if not user or user.is_bot or user.is_self or user.id in ADMINS:
        return False
    if message.outgoing or has_link(message):
        return False
    text = message.text.strip()
    if len(text) < 2 or len(text) > MAX_TEXT or not re.search(r"[^\W\d_]", text):
        return False
    # A reply to another (non-admin) member is a conversation between users, not a request to us.
    reply = message.reply_to_message
    if reply is not None and reply.from_user:
        ru = reply.from_user
        if not ru.is_self and not ru.is_bot and ru.id not in ADMINS:
            return False
    return True


support_filter = filters.create(_support_filter)


def _prune():
    if len(_SEEN_TEXT) > 5000:
        cutoff = time.time() - 600
        for k in [k for k, t in _SEEN_TEXT.items() if t < cutoff]:
            _SEEN_TEXT.pop(k, None)
    if len(_LAST_USER) > 5000:
        cutoff = time.time() - 60
        for k in [k for k, t in _LAST_USER.items() if t < cutoff]:
            _LAST_USER.pop(k, None)


@Client.on_message(filters.text & filters.group & support_filter, group=1)
async def support_message(bot, message):
    text = message.text.strip()
    user = message.from_user
    now = time.time()
    if now - _LAST_USER.get(user.id, 0) < 3:          # flood guard
        return
    _LAST_USER[user.id] = now
    seen_key = (message.chat.id, user.id, text.lower())
    if now - _SEEN_TEXT.get(seen_key, 0) < 600:        # same text again within 10 minutes
        return
    _SEEN_TEXT[seen_key] = now
    _prune()

    try:
        ai = await detect_title(text)
        fallback = ai is None
        if fallback:
            # No AI answered. Only a short message that matches a title exactly is trusted;
            # anything else could be chit-chat, so stay silent rather than bother the admins.
            if len(text.split()) > 6:
                return
            ai = {"is_title": True, "title": text, "year": "", "season": None, "language": "", "confidence": 75}
        if not ai["is_title"] or not ai["title"]:
            return

        loc = await resolve(ai["title"], ai["year"], ai["season"])
        sure = ai["confidence"] >= CONF_MIN

        if loc["state"] == "found" and sure:
            await _send_card(bot, message.chat.id, message.id, user.id, user.first_name,
                             loc["results"], ai["title"])
            return

        if fallback:
            return
        if loc["state"] == "found":
            reason = f"AI is not sure ({ai['confidence']}%), but the database has files for this title"
        elif loc["state"] == "partial":
            reason = f"Title is in the database but Season {ai['season']} was not found"
        elif loc["state"] == "ambiguous":
            reason = "Several different titles match this name"
        else:
            reason = "Not found in the database" if sure else f"Not found in the database and AI is not sure ({ai['confidence']}%)"
        await _create_ask(bot, message, ai, reason)
    except Exception:
        logger.exception("Support-group message failed: %r", text[:80])
