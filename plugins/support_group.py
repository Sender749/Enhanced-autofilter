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
    is_support_chat, SUPPORT_REPORT_ALL,
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
    "- If several different titles are named, use the first one and give confidence 50 or less.\n"
    "- Users add extra words around the title: bro, bhai, sir, pls, full, all episodes, complete, season N, "
    "hindi, 1080p, link, upload kar do. Drop them from the title; use them only for season / year.\n\n"
    "Examples:\n"
    "\"Bro moon knight\" -> {\"is_title\": true, \"title\": \"Moon Knight\", \"year\": \"\", \"season\": null, \"language\": \"\", \"confidence\": 90}\n"
    "\"Dupahiya season 2 Full episodes\" -> {\"is_title\": true, \"title\": \"Dupahiya\", \"year\": \"\", \"season\": 2, \"language\": \"\", \"confidence\": 85}\n"
    "\"Dopahiya season 1\" -> {\"is_title\": true, \"title\": \"Dupahiya\", \"year\": \"\", \"season\": 1, \"language\": \"\", \"confidence\": 70}\n"
    "\"bot kaam nahi kar raha\" -> {\"is_title\": false, \"title\": \"\", \"year\": \"\", \"season\": null, \"language\": \"\", \"confidence\": 95}\n\n"
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
                    logger.warning("Groq title detection: HTTP %s %s", r.status, (await r.text())[:200])
                    return None
                data = await r.json()
        return _parse_ai(data["choices"][0]["message"]["content"])
    except Exception:
        logger.warning("Groq title detection failed", exc_info=True)
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
                    logger.warning("Gemini title detection: HTTP %s %s", r.status, (await r.text())[:200])
                    return None
                data = await r.json()
        return _parse_ai(data["candidates"][0]["content"]["parts"][0]["text"])
    except Exception:
        logger.warning("Gemini title detection failed", exc_info=True)
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


# ── Extra words around the title ("Bro moon knight", "Dupahiya season 2 Full episodes") ──

_FILLER = {
    "bro", "bhai", "bhaiya", "sir", "sis", "dude", "yaar", "pls", "plz", "please", "pleaseee", "hi", "hello", "hey",
    "full", "complete", "all", "every", "episode", "episodes", "ep", "eps", "season", "seasons", "series", "web",
    "movie", "movies", "film", "show", "hindi", "dubbed", "dual", "audio", "english", "hd", "link", "links",
    "send", "give", "dedo", "do", "de", "chahiye", "chahie", "hai", "he", "h", "kya", "ka", "ki", "ke", "ko", "me",
    "mein", "upload", "uploaded", "kar", "karo", "krdo", "kardo", "download", "want", "need", "available", "watch",
    "the", "a", "an", "of", "in", "is", "any", "one", "new", "latest", "mujhe", "muje", "aap", "app", "bot",
}


def _local_candidates(text: str):
    """-> (candidate titles, season or None) built only from the user's own words:
    filler / season words removed, then the whole phrase and its shorter word windows."""
    t = text.lower()
    season = None
    m = re.search(r"\bseason\s*0*(\d{1,2})\b", t) or re.search(r"\bs0*(\d{1,2})\b", t)
    if m:
        season = int(m.group(1))
        t = t[:m.start()] + " " + t[m.end():]
    words = [w for w in re.findall(r"[^\W_]+", t) if w not in _FILLER and not re.fullmatch(r"\d{3,4}p", w)]
    if not words:
        return [], season
    cands = [" ".join(words)]
    n = len(words)
    for size in range(n - 1, 1, -1):          # windows of 2+ words only (a single word is too risky)
        for i in range(0, n - size + 1):
            cands.append(" ".join(words[i:i + size]))
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:10], season


async def _quick_exact(title: str, season=None):
    """Cheap exact-title check used for the shorter word windows."""
    results = await search_files(f"{title} s{int(season):02d}" if season else title) or await search_files(title)
    if not results:
        return None
    return await _exact_title_files(title, results)


async def resolve_message(ai: dict | None, text: str) -> dict:
    """Look the message up: the AI's title first, then the user's own words with the filler removed.
    -> resolve() dict, plus "title" (what matched)."""
    ai_title = (ai or {}).get("title", "")
    year = (ai or {}).get("year", "")
    ai_season = (ai or {}).get("season")
    best = {"state": "none", "results": [], "via": "exact", "title": ai_title}

    if ai_title:
        best = await resolve(ai_title, year, ai_season)
        best["title"] = ai_title
        if best["state"] == "found":
            return best

    cands, local_season = _local_candidates(text)
    season = ai_season or local_season
    for i, cand in enumerate(cands):
        if cand.lower() == ai_title.lower():
            continue
        if i == 0:
            loc = await resolve(cand, "", season)
        else:
            files = await _quick_exact(cand, season)
            loc = {"state": "found", "results": files, "via": "local"} if files else {"state": "none", "results": []}
            if files and season and not any(
                    extract_media_info(f.get("file_name", ""), _plain(f.get("caption", "") or ""))["season"] == int(season)
                    for f in files):
                loc["state"] = "partial"
        if loc["state"] in ("found", "partial") and loc["results"]:
            loc["title"] = cand
            if loc["state"] == "found" or best["state"] not in ("found", "partial"):
                best = loc
            if loc["state"] == "found":
                return best
    return best


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
    """Words for the /start sgfile-<words> deep link (payload cap is 64 chars)."""
    words = re.sub(r"[^A-Za-z0-9_\s]", " ", title).split()
    out = ""
    for w in words:
        candidate = f"{out}-{w}" if out else w
        if len(candidate) > 56:
            break
        out = candidate
    return out


async def build_card(results: list, fallback_title: str, header: str, query: str | None = None):
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

    query = _getfiles_query(query or title)
    link = f"https://t.me/{temp.U_NAME}?start=sgfile-{query}" if query else f"https://t.me/{temp.U_NAME}"
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

OPEN, WAIT_SP, WAIT_CU, WAIT_QRY, DONE, IGNORED = "open", "waiting_sp", "waiting_cu", "waiting_qry", "done", "ignored"
WAITING = [WAIT_SP, WAIT_CU, WAIT_QRY]


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


def _query_markup(ask_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Use default", callback_data=f"sga#df#{ask_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"sga#cn#{ask_id}"),
    ]])


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
                               "state": {"$in": [OPEN] + WAITING}})
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

LABELS = {"up": "✅ ᴀʟʀᴇᴀᴅʏ ᴜᴘʟᴏᴀᴅᴇᴅ", "nw": "🆕 ɴᴏᴡ ᴜᴘʟᴏᴀᴅᴇᴅ"}
RESULT_TEXT = {"up": "✅ Already uploaded", "nw": "🆕 Now uploaded", "sp": "✏️ Spelling mistake", "cu": "💬 Custom message"}


def _default_query(doc: dict) -> str:
    """What the Get Files button searches when the admin presses 'Use default'."""
    return doc.get("spelling") or doc.get("title") or ""


def _files_markup(query: str, with_group: bool = True):
    rows = []
    q = _getfiles_query(query or "")
    if q:
        rows.append([InlineKeyboardButton("🔍 ɢᴇᴛ ꜰɪʟᴇs", url=f"https://t.me/{temp.U_NAME}?start=sgfile-{q}")])
    if with_group and MOVIE_GROUP_LINK:
        rows.append([InlineKeyboardButton("🎬 Open Main Group", url=MOVIE_GROUP_LINK)])
    return InlineKeyboardMarkup(rows) if rows else None


async def _card_or_none(bot, doc: dict, query: str, header: str):
    """Full card when the query is in the database, else None."""
    if not query:
        return None
    try:
        loc = await resolve(query)
    except Exception:
        logger.exception("Lookup failed while answering the support group")
        return None
    if loc["state"] in ("found", "partial") and loc["results"]:
        text, poster, markup = await build_card(loc["results"], query, header, query=query)
        return await _send(bot, doc["chat_id"], doc["msg_id"], text, markup, poster["url"] if poster else None)
    return None


async def _relay(bot, doc: dict, query: str):
    """The admin's decision (doc['action']) is sent to the support group; `query` feeds Get Files."""
    action = doc["action"]
    name = _mention(doc["user_id"], doc.get("user_name"))

    if action in ("up", "nw"):
        header = f"{name} 👋\n<b>{LABELS[action]}</b>\n\n"
        sent = await _card_or_none(bot, doc, query, header)
        if sent:
            return sent
        what = f"<b>{html.escape(query)}</b>" if query else "the movie / series you asked for"
        text = f"{header}{what} is available. Tap the button below 👇"
        return await _send(bot, doc["chat_id"], doc["msg_id"], text, _files_markup(query))

    if action == "sp":
        spelling = doc.get("spelling", "")
        header = (f"{name} 👋\n<b>✏️ ʏᴏᴜʀ sᴘᴇʟʟɪɴɢ ɪs ᴡʀᴏɴɢ</b>\n"
                  f"ᴄᴏʀʀᴇᴄᴛ ɴᴀᴍᴇ: <b>{html.escape(spelling)}</b>\n\n")
        sent = await _card_or_none(bot, doc, query, header)
        if sent:
            return sent
        text = header + "Tap the button below, or send this name in the main group 👇"
        return await _send(bot, doc["chat_id"], doc["msg_id"], text, _files_markup(query))

    # custom message
    text = f"{name} 👋\n\n{html.escape(doc.get('custom', ''))}"
    return await _send(bot, doc["chat_id"], doc["msg_id"], text, _files_markup(query, with_group=False))


async def _finish(bot, doc: dict, query: str):
    """Final step: send to the support group and close the report."""
    sent = await _relay(bot, doc, query)
    label = RESULT_TEXT[doc["action"]]
    if doc["action"] == "sp":
        label += f" → <code>{html.escape(doc.get('spelling', ''))}</code>"
    q = f" · 🔍 <code>{html.escape(query)}</code>" if query else " · no Get Files button"
    tail = "sent to the support group" if sent else "⚠️ could not post in the support group"
    await _delete_quietly(bot, doc.get("prompt_id"))
    await _edit(bot, doc, f"{_report_text(doc)}\n\n<b>{label}</b>{q} — {tail}.")
    if not sent:
        await _notice(bot, "⚠️ Couldn't post in the support group. Is the bot still an admin there?")


async def _ask_query(bot, doc: dict):
    """Ask the admin which words the Get Files button should search."""
    default = _default_query(doc)
    hint = f"\nDefault: <code>{html.escape(default)}</code>" if default else "\nNo default — Use default sends no button."
    try:
        prompt = await bot.send_message(
            NOT_FOUND_FILE_CHANNEL,
            "🔍 Reply to this message with the <b>search query</b> for the Get Files button "
            "(what the user will see when they tap it)." + hint,
            parse_mode=HTML, reply_to_message_id=doc["nf_msg_id"], reply_markup=_query_markup(str(doc["_id"])))
        await asks.update_one({"_id": doc["_id"]}, {"$set": {"prompt_id": prompt.id}})
    except Exception:
        logger.exception("Could not post the query prompt")
    await _edit(bot, doc, f"{_report_text(doc)}\n\n⏳ <i>Waiting for the search query…</i>", _query_markup(str(doc["_id"])))


# ── buttons ─────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^sga#(up|nw|sp|cu|ig|cn|df)#"))
async def support_buttons(bot, query):
    if query.from_user.id not in ADMINS:
        await query.answer("Admins only.", show_alert=True)
        return

    _, action, ask_id = query.data.split("#", 2)
    handled = "Already handled by another admin."

    if action in ("up", "nw"):
        doc = await _transition(ask_id, [OPEN], WAIT_QRY, {"action": action})
        if not doc:
            return await query.answer(handled, show_alert=True)
        await query.answer("Reply with the search query.")
        return await _ask_query(bot, doc)

    if action in ("sp", "cu"):
        waiting = WAIT_SP if action == "sp" else WAIT_CU
        doc = await _transition(ask_id, [OPEN], waiting, {"action": action})
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

    if action == "df":
        doc = await _transition(ask_id, [WAIT_QRY], DONE)
        if not doc:
            return await query.answer(handled, show_alert=True)
        await query.answer("Sending…")
        return await _finish(bot, doc, _default_query(doc))

    if action == "cn":
        doc = await _transition(ask_id, WAITING, OPEN)
        if not doc:
            return await query.answer(handled, show_alert=True)
        await _delete_quietly(bot, doc.get("prompt_id"))
        await asks.update_one({"_id": doc["_id"]}, {"$unset": {"action": "", "spelling": "", "custom": "", "prompt_id": ""}})
        await _edit(bot, doc, _report_text(doc), _open_markup(ask_id))
        return await query.answer("Cancelled.")

    if action == "ig":
        doc = await _transition(ask_id, [OPEN] + WAITING, IGNORED)
        if not doc:
            return await query.answer(handled, show_alert=True)
        await _delete_quietly(bot, doc.get("prompt_id"))
        await _edit(bot, doc, f"{_report_text(doc)}\n\n🚫 <b>Ignored.</b>")
        return await query.answer("Ignored.")


# ── the admin's reply in the channel (spelling / custom text / search query) ──

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
    doc = await asks.find_one({"state": {"$in": WAITING}, "$or": [{"nf_msg_id": rid}, {"prompt_id": rid}]})
    if not doc:
        message.continue_propagation()

    try:
        typed = (message.text or "").strip()
        if not typed:
            return
        was = doc["state"]

        if was == WAIT_QRY:
            if not _getfiles_query(typed):
                await _notice(bot, "⚠️ The search query needs English letters or numbers.")
                return
            claimed = await _transition(str(doc["_id"]), [WAIT_QRY], DONE)
            if not claimed:
                return
            await _delete_quietly(bot, message.id)
            await _finish(bot, claimed, typed)
            return

        # spelling or custom text: keep it, then ask for the search query
        field = "spelling" if was == WAIT_SP else "custom"
        claimed = await _transition(str(doc["_id"]), [was], WAIT_QRY, {field: typed})
        if not claimed:
            return
        await _delete_quietly(bot, message.id, claimed.get("prompt_id"))
        await _ask_query(bot, claimed)
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

    ai = None
    try:
        ai = await detect_title(text)
        ai_down = ai is None
        if ai_down:
            logger.warning("Support group: no AI answered, using the local word match for %r", text[:60])
        elif not ai["is_title"] or not ai["title"]:
            logger.info("Support group: not a title request: %r", text[:60])
            if SUPPORT_REPORT_ALL:
                await _create_ask(bot, message, ai, "AI thinks this is not a movie / series request")
            return

        loc = await resolve_message(ai, text)
        sure = (not ai_down) and ai["confidence"] >= CONF_MIN
        if ai_down:   # nothing but the user's own words: trust an exact database match only
            sure = loc["state"] == "found" and loc.get("via") in ("exact", "phrase", "local")
        logger.info("Support group: %r -> ai=%s state=%s via=%s sure=%s", text[:60],
                    (ai or {}).get("title"), loc["state"], loc.get("via"), sure)

        if loc["state"] == "found" and sure:
            await _send_card(bot, message.chat.id, message.id, user.id, user.first_name,
                             loc["results"], loc.get("title") or (ai or {}).get("title") or text)
            return
        season = (ai or {}).get("season")
        if loc["state"] == "found":
            reason = f"AI is not sure ({ai['confidence']}%), but the database has files for this title"
        elif loc["state"] == "partial":
            reason = f"Title is in the database but Season {season} was not found" if season else "Title is in the database but the asked season was not found"
        elif loc["state"] == "ambiguous":
            reason = "Several different titles match this name"
        elif ai_down:
            reason = "AI did not answer and the message was not found in the database"
        else:
            reason = "Not found in the database" if sure else f"Not found in the database and AI is not sure ({ai['confidence']}%)"
        if ai_down and len(text.split()) > 8:
            return                      # a long message with no AI is almost surely just chat
        await _create_ask(bot, message, ai or {"title": "", "year": "", "season": None, "confidence": 0}, reason)
    except Exception:
        logger.exception("Support-group message failed: %r", text[:80])
        # Never stay silent on a failure: let the admins see the message.
        try:
            await _create_ask(bot, message, ai or {"title": "", "year": "", "season": None, "confidence": 0},
                              "The bot hit an error while checking this message")
        except Exception:
            logger.exception("Could not even post the report")
