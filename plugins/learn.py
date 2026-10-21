"""
AI learning — the bot learns from what users do and suggests improvements;
admins approve with one tap in the #FILE_NOT_FOUND channel.

What it watches (see database/learn_db.py; users are stored as salted hashes):
  * a search that found nothing, then the user searched again and it worked
  * a search that found nothing, then the user tapped a suggestion / related title
  * requests (what people want but the database doesn't have)

What it suggests (each as ONE message with buttons — nothing changes by itself):
  * alias        "hindi madium" -> "Hindi Medium"      (several different users agree)
  * filter word  "download"       dropped from queries (removing it makes failed searches work)
  * ignore word  "ToonWorld4All"  uploader/channel name at the start of many titles

Optional AI judge (/settings -> AI Learning -> AI judge): Groq / Gemini / TMDB
double-check an alias before it is suggested. They never invent anything: a
suggestion is only made for titles that really exist in the database.

Everything is adjustable from /settings -> 🧠 AI Learning (plugins/learn_panel.py).
"""
import asyncio
import html
import json
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import aiohttp
from pyrogram import Client, enums, filters
from pyrogram.errors import RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from rapidfuzz import fuzz

from config import (
    ADMINS, NOT_FOUND_FILE_CHANNEL, AI_FETCH_TIMEOUT,
    GROQ_API_KEY, GROQ_MODEL, GEMINI_API_KEY, GEMINI_MODEL, TMDB_API_KEY,
)
from database import learn_db as L
from database.alias_db import (
    alias_key, get_alias, save_alias, delete_alias, create_miss, set_miss_message,
    transition as miss_transition, other_open_with_key,
)
from database.filters_db import (
    _JUNK_RE, _tokenize, clean_title, display_name, files, rekey_docs_with_word,
    parse_tag_only_query, search_files, set_noise_words, get_noise_words,
)
from database.settings_db import get_settings, update_settings
from filterwords import apply_filter_words

logger = logging.getLogger(__name__)
HTML = enums.ParseMode.HTML

RETRY_WINDOW = 150          # seconds: "searched again" must follow the miss this closely
RETRY_MIN_SIMILARITY = 55   # the second search must look like the first (0-100)
CYCLE_SECONDS = 900         # how often the engine looks for new suggestions
MAX_PER_CYCLE = 3           # never flood the channel, whatever the daily cap
NOISE_SCAN_EVERY = 24 * 3600


# ══════════════════════════════════════════════════════════════════════════════
# Observing — tiny hooks called from search.py / request.py. Fire-and-forget:
# a failure here must never affect a user's search.
# ══════════════════════════════════════════════════════════════════════════════

_LAST_MISS: dict = {}  # user_id -> {"query", "qkey", "time"}


def _prune_last_miss():
    if len(_LAST_MISS) > 5000:
        cutoff = time.time() - RETRY_WINDOW
        for uid in [u for u, v in _LAST_MISS.items() if v["time"] < cutoff]:
            _LAST_MISS.pop(uid, None)


def _dominant_title(results: list) -> str:
    titles = Counter(clean_title(display_name(d)) for d in results)
    titles.pop("", None)
    return titles.most_common(1)[0][0] if titles else ""


async def _active(flag: str | None, user_id) -> bool:
    if not user_id or user_id in ADMINS:  # admins test the bot; that's not user behaviour
        return False
    settings = await get_settings()
    return bool(settings["ai_learning_enabled"] and (flag is None or settings[flag]))


async def note_miss(user_id, query: str):
    """The user got no result (not even suggestions that exist in the database)."""
    try:
        if not await _active(None, user_id):
            return
        qkey = alias_key(query)
        if not qkey:
            return
        _LAST_MISS[user_id] = {"query": query, "qkey": qkey, "time": time.time()}
        _prune_last_miss()
        await L.add_event("miss", user_id, qkey, query)
    except Exception:
        logger.debug("note_miss failed", exc_info=True)


async def note_success(user_id, results: list):
    """A normal search found files. If the SAME user just had a miss and this
    looks like a retry of it, that's "typed A, meant B"."""
    try:
        last = _LAST_MISS.get(user_id)
        if not last:
            return
        if time.time() - last["time"] > RETRY_WINDOW:
            _LAST_MISS.pop(user_id, None)
            return
        if not await _active("ai_learn_retry", user_id):
            return
        title = _dominant_title(results)
        tkey = alias_key(title)
        if not tkey or tkey == last["qkey"]:
            _LAST_MISS.pop(user_id, None)
            return
        if fuzz.ratio(last["qkey"], tkey) < RETRY_MIN_SIMILARITY:
            return  # probably a different movie — keep waiting for a closer retry
        _LAST_MISS.pop(user_id, None)
        await L.add_event("pair", user_id, last["qkey"], last["query"], tkey, title, "retry")
    except Exception:
        logger.debug("note_success failed", exc_info=True)


async def note_tap(user_id, query: str, title: str):
    """The user tapped a suggestion / related title after their search failed."""
    try:
        _LAST_MISS.pop(user_id, None)
        if not await _active("ai_learn_taps", user_id):
            return
        qkey, tkey = alias_key(query), alias_key(title)
        if not qkey or not tkey or qkey == tkey:
            return
        await L.add_event("pair", user_id, qkey, query, tkey, title, "tap")
    except Exception:
        logger.debug("note_tap failed", exc_info=True)


async def note_request(user_id, query: str):
    try:
        if not await _active("ai_learn_requests", user_id):
            return
        qkey = alias_key(query)
        if qkey:
            await L.add_event("request", user_id, qkey, query)
    except Exception:
        logger.debug("note_request failed", exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
# AI judge — optional second opinion on an alias (Groq / Gemini / TMDB)
# ══════════════════════════════════════════════════════════════════════════════

_JUDGE_PROMPT = (
    "A Telegram movie bot's user searched for \"{query}\" (it found nothing), and then opened "
    "the title \"{target}\". Are these the SAME movie / TV series / anime, allowing for spelling "
    "mistakes, transliteration, spacing, and missing or extra small words? Different sequels or "
    "seasons are NOT the same. Reply with JSON only: {{\"same\": true or false, \"confidence\": 0-100}}"
)


def _parse_verdict(text: str):
    match = re.search(r"\{.*?\}", text, re.S)
    if not match:
        return None, 0
    try:
        data = json.loads(match.group(0))
        return bool(data.get("same")), int(data.get("confidence", 0))
    except (ValueError, TypeError):
        return None, 0


async def _llm_same(query: str, target: str):
    prompt = _JUDGE_PROMPT.format(query=query, target=target)
    timeout = aiohttp.ClientTimeout(total=AI_FETCH_TIMEOUT)
    if GROQ_API_KEY:
        try:
            payload = {"model": GROQ_MODEL, "temperature": 0, "max_tokens": 60,
                       "messages": [{"role": "user", "content": prompt}]}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post("https://api.groq.com/openai/v1/chat/completions", json=payload,
                                        headers={"Authorization": f"Bearer {GROQ_API_KEY}"}) as r:
                    if r.status == 200:
                        data = await r.json()
                        verdict = _parse_verdict(data["choices"][0]["message"]["content"])
                        if verdict[0] is not None:
                            return verdict
        except Exception:
            logger.debug("Groq judge failed", exc_info=True)
    if GEMINI_API_KEY:
        try:
            payload = {"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"temperature": 0, "maxOutputTokens": 80}}
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers={
                        "x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}) as r:
                    if r.status == 200:
                        data = await r.json()
                        return _parse_verdict(data["candidates"][0]["content"]["parts"][0]["text"])
        except Exception:
            logger.debug("Gemini judge failed", exc_info=True)
    return None, 0


async def _tmdb_titles(query: str) -> list:
    if not TMDB_API_KEY:
        return []
    params = {"api_key": TMDB_API_KEY, "query": query, "include_adult": "false"}
    timeout = aiohttp.ClientTimeout(total=AI_FETCH_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://api.themoviedb.org/3/search/multi", params=params) as r:
                if r.status != 200:
                    return []
                data = await r.json()
    except Exception:
        return []
    out = []
    for item in data.get("results", [])[:6]:
        if item.get("media_type") in ("movie", "tv"):
            out += [t for t in (item.get("title"), item.get("name"), item.get("original_title"),
                                item.get("original_name")) if t]
    return out


async def judge_alias(query: str, target: str):
    """-> (True | False | None, short note). None = the AIs couldn't decide."""
    tkey = alias_key(target)
    tmdb_agrees = any(fuzz.ratio(alias_key(t), tkey) >= 92 for t in await _tmdb_titles(query))
    same, confidence = await _llm_same(query, target)
    if tmdb_agrees:
        return True, "TMDB agrees"
    if same is True and confidence >= 70:
        return True, f"AI judge: same title ({confidence}%)"
    if same is False and confidence >= 70:
        return False, f"AI judge: different titles ({confidence}%)"
    return None, "AI judge unsure"


# ══════════════════════════════════════════════════════════════════════════════
# Finding suggestions
# ══════════════════════════════════════════════════════════════════════════════

_EVALUATED: dict = {}  # (type, key) -> time, so a rejected-by-evidence idea isn't re-tested every cycle
_EVALUATED_TTL = 6 * 3600


def _recently_evaluated(ctype: str, key: str) -> bool:
    t = _EVALUATED.get((ctype, key))
    return bool(t and time.time() - t < _EVALUATED_TTL)


def _mark_evaluated(ctype: str, key: str):
    if len(_EVALUATED) > 5000:
        _EVALUATED.clear()
    _EVALUATED[(ctype, key)] = time.time()


async def _can_create(ctype: str, key: str, target: str = ""):
    """None if this idea is blocked / already handled; else the existing pending
    doc (or False when there is none yet)."""
    if await L.is_blocked(ctype, key, target):
        return None
    existing = await L.cand_find(ctype, key, target)
    if existing and existing["state"] != "pending":
        return None
    return existing or False


async def _generate_aliases(settings: dict, limit: int) -> int:
    min_users = settings["ai_min_users"]
    use_llm = settings["ai_use_llm"]
    need = max(1, min_users - 1) if use_llm else min_users
    made = judged = 0
    for g in sorted(await L.pair_groups(), key=lambda g: -g["users"]):
        if made >= limit:
            break
        if g["users"] < need or _recently_evaluated("alias", g["qkey"] + ">" + g["tkey"]):
            continue
        state = await _can_create("alias", g["qkey"], g["tkey"])
        if state is None:
            continue
        _mark_evaluated("alias", g["qkey"] + ">" + g["tkey"])
        if await get_alias(g["qkey"]):
            continue
        if await search_files(g["query"] or g["qkey"], use_alias=False):
            continue  # no longer a miss
        found = await search_files(g["target"], use_alias=False)
        if not found:
            continue

        note = ""
        if use_llm and judged < 5:
            judged += 1
            verdict, note = await judge_alias(g["query"] or g["qkey"], g["target"])
            if verdict is False or (verdict is None and g["users"] < min_users):
                continue
        elif use_llm and g["users"] < min_users:
            continue

        if state is False:
            await L.cand_create("alias", g["qkey"], g["tkey"], {
                "title": g["target"], "query": g["query"], "users": g["users"], "files": len(found),
                "score": g["users"], "note": note, "sources": sorted(g["sources"]),
            })
            made += 1
    return made


async def _generate_filterwords(settings: dict, limit: int) -> int:
    min_users = settings["ai_min_users"]
    existing = {w.lower() for w in settings["filter_words"]}
    # Words of searches that are really SPELLING mistakes (they have an alias idea)
    # are never filter words: "madium" is a typo of "medium", not noise.
    typo_words = {w for g in await L.pair_groups() for w in _tokenize(g["qkey"])}
    word_users, word_queries = defaultdict(set), defaultdict(list)
    for ev in await L.miss_events():
        for w in set(_tokenize(ev.get("query", ""))):
            if len(w) >= 3 and not w.isdigit():
                word_users[w].add(ev["u"])
                if len(word_queries[w]) < 20:
                    word_queries[w].append(ev["query"])

    made = checked = 0
    for w, users in sorted(word_users.items(), key=lambda kv: -len(kv[1])):
        if made >= limit or checked >= 6:
            break
        if len(users) < min_users or w in existing or w in typo_words or _recently_evaluated("filterword", w):
            continue
        state = await _can_create("filterword", w)
        if state is None:
            continue
        checked += 1
        _mark_evaluated("filterword", w)
        if await search_files(w, use_alias=False):
            continue  # it IS a title on its own — never a filter word
        queries = word_queries[w]
        hits, examples = 0, []
        for q in queries:
            stripped = apply_filter_words(q, [w])
            # what is left must be a real TITLE search that works (not just a language/year tag)
            if (stripped and stripped.lower() != q.lower() and parse_tag_only_query(stripped) is None
                    and await search_files(stripped, use_alias=False)):
                hits += 1
                if len(examples) < 3:
                    examples.append(q)
        if hits < 2 or hits / len(queries) < 0.4:
            continue
        if state is False:
            await L.cand_create("filterword", w, "", {
                "title": w, "users": len(users), "hits": hits, "total": len(queries),
                "examples": examples, "score": len(users),
            })
            made += 1
    return made


def _scan_noise_batch(batch: list, acc: dict):
    seen = acc["seen"]
    for doc in batch:
        title = clean_title(display_name(doc))
        low = title.lower()
        if not low or low in seen:
            continue
        seen.add(low)
        tokens = re.findall(r"[a-z0-9]+", low)
        if not tokens:
            continue
        acc["first"][tokens[0]].append(title) if len(acc["first"][tokens[0]]) < 3 else None
        acc["first_n"][tokens[0]] += 1
        for t in set(tokens):
            acc["any_n"][t] += 1
        if len(tokens) == 1:
            acc["whole"].add(tokens[0])
        elif len(acc["next"][tokens[0]]) < 60:
            acc["next"][tokens[0]].add(tokens[1])


async def _generate_noise(settings: dict, limit: int) -> int:
    last = await L.meta_get("noise_scan_at", 0)
    if time.time() - last < NOISE_SCAN_EVERY:
        return 0
    await L.meta_set("noise_scan_at", time.time())

    acc = {"seen": set(), "first": defaultdict(list), "first_n": Counter(), "any_n": Counter(),
           "whole": set(), "next": defaultdict(set)}
    batch = []
    async for doc in files.find({}, {"file_name": 1, "caption": 1}).limit(300000):
        batch.append(doc)
        if len(batch) >= 5000:
            await asyncio.to_thread(_scan_noise_batch, batch, acc)
            batch = []
    if batch:
        await asyncio.to_thread(_scan_noise_batch, batch, acc)

    known = get_noise_words()
    made = 0
    for tok, n in acc["first_n"].most_common(200):
        if made >= limit * 10:  # the scan may queue many; posting is rate-limited separately
            break
        if (n < 25 or len(tok) < 4 or tok.isdigit() or tok in acc["whole"] or tok in known
                or _JUNK_RE.match(tok)):
            continue
        if len(acc["next"][tok]) < 15 or n / max(1, acc["any_n"][tok]) < 0.9:
            continue
        if await _can_create("noise", tok) is not False:
            continue
        await L.cand_create("noise", tok, "", {
            "title": tok, "users": n, "names": len(acc["next"][tok]),
            "examples": acc["first"][tok][:3], "score": n / 100,
        })
        made += 1
    return made


async def _post_pending(bot, settings: dict, limit: int) -> int:
    allowed = [t for t, flag in (("alias", "ai_type_alias"), ("filterword", "ai_type_filterword"),
                                 ("noise", "ai_type_noise")) if settings[flag]]
    if not allowed or limit <= 0:
        return 0
    pending = await L.cands.find({"state": "pending", "type": {"$in": allowed}}).sort("score", -1) \
        .limit(limit * 3).to_list(length=limit * 3)
    posted = 0
    for cand in pending:
        if posted >= limit:
            break
        if await L.is_blocked(cand["type"], cand["key"], cand["target"]):
            continue
        if cand["type"] == "alias" and await get_alias(cand["key"]):
            await L.cand_transition(str(cand["_id"]), ["pending"], "undone")
            continue
        if await _post(bot, cand):
            posted += 1
    return posted


async def run_cycle(bot) -> int:
    """One pass: look for new suggestions, then post a few. Returns how many were posted."""
    settings = await get_settings()
    if not settings["ai_learning_enabled"]:
        return 0
    await L.ensure_indexes()
    if settings["ai_type_alias"]:
        await _generate_aliases(settings, 5)
    if settings["ai_type_filterword"]:
        await _generate_filterwords(settings, 3)
    if settings["ai_type_noise"]:
        await _generate_noise(settings, 3)
    room = settings["ai_daily_cap"] - await L.posted_since(24)
    return await _post_pending(bot, settings, min(MAX_PER_CYCLE, room))


async def learning_loop(bot):
    await asyncio.sleep(120)  # let the bot finish starting
    while True:
        try:
            await run_cycle(bot)
        except Exception:
            logger.exception("AI learning cycle failed")
        await asyncio.sleep(CYCLE_SECONDS)


def start_learning(bot):
    asyncio.create_task(learning_loop(bot))


async def load_noise_words():
    set_noise_words(await L.load_noise())


# ══════════════════════════════════════════════════════════════════════════════
# The suggestion messages (in the #FILE_NOT_FOUND channel)
# ══════════════════════════════════════════════════════════════════════════════

def _render(cand: dict, state: str):
    ctype, cid = cand["type"], str(cand["_id"])
    e = html.escape
    if ctype == "alias":
        body = (
            "🧠 <b>AI suggestion · alias</b>\n\n"
            f"Users searched <code>{e(cand.get('query') or cand['key'])}</code> and then opened\n"
            f"<b>{e(cand['title'])}</b>\n\n"
            f"👥 {cand['users']} users · 📁 {cand.get('files', '?')} files"
            + (f" · 🤖 {e(cand['note'])}" if cand.get("note") else "")
            + f"\n\nAdd alias <code>{e(cand['key'])}</code> → <b>{e(cand['title'])}</b>?"
        )
        approve = "✅ Approve"
    elif ctype == "filterword":
        examples = "\n".join(f"• <code>{e(x)}</code>" for x in cand.get("examples", []))
        body = (
            "🧠 <b>AI suggestion · filter word</b>\n\n"
            f"<code>{e(cand['key'])}</code> is in the failed searches of {cand['users']} users. Without it, "
            f"{cand['hits']} of {cand['total']} of those searches would have found files.\n\n"
            f"{examples}\n\nAdd it as a filter word (dropped from every query)?"
        )
        approve = "✅ Add filter word"
    else:
        examples = "\n".join(f"• {e(x)}" for x in cand.get("examples", []))
        body = (
            "🧠 <b>AI suggestion · ignore word</b>\n\n"
            f"<code>{e(cand['key'])}</code> starts {cand['users']} different titles "
            f"({cand['names']}+ different names follow it), for example:\n{examples}\n\n"
            "Ignore it at the start of titles when matching?"
        )
        approve = "✅ Ignore word"

    if state == "posted":
        row = [InlineKeyboardButton(approve, callback_data=f"ail#ok#{cid}")]
        if ctype == "alias":
            row.append(InlineKeyboardButton("✏️ Change", callback_data=f"ail#chg#{cid}"))
        row.append(InlineKeyboardButton("🚫 Reject", callback_data=f"ail#no#{cid}"))
        return body, InlineKeyboardMarkup([row])
    if state == "approved":
        return body + "\n\n✅ <b>Approved</b>", InlineKeyboardMarkup(
            [[InlineKeyboardButton("🗑 Undo", callback_data=f"ail#undo#{cid}")]])
    if state == "rejected":
        return body + "\n\n🚫 <b>Rejected</b> — won't be suggested again.", None
    if state == "undone":
        return body + "\n\n↩️ <b>Undone</b> — removed and won't be suggested again.", None
    return body, None


async def _post(bot, cand: dict) -> bool:
    if not NOT_FOUND_FILE_CHANNEL:
        return False
    text, markup = _render(cand, "posted")
    try:
        sent = await bot.send_message(NOT_FOUND_FILE_CHANNEL, text, parse_mode=HTML, reply_markup=markup)
    except Exception:
        logger.exception("Could not post AI suggestion")
        return False
    await L.cand_set(cand["_id"], {"state": "posted", "msg_id": sent.id, "posted_at": datetime.now(timezone.utc)})
    return True


async def _edit_cand_message(bot, cand: dict, state: str):
    if not (NOT_FOUND_FILE_CHANNEL and cand.get("msg_id")):
        return
    text, markup = _render(cand, state)
    try:
        await bot.edit_message_text(NOT_FOUND_FILE_CHANNEL, cand["msg_id"], text, parse_mode=HTML, reply_markup=markup)
    except RPCError as exc:
        logger.debug("Could not edit AI suggestion %s: %s", cand.get("msg_id"), exc)


# ══════════════════════════════════════════════════════════════════════════════
# Applying / undoing a suggestion
# ══════════════════════════════════════════════════════════════════════════════

async def _rekey(word: str):
    """Existing files follow a new/removed ignore word right away. If this step
    fails the approval still stands — the startup rebuild picks the files up."""
    try:
        await rekey_docs_with_word(word)
    except Exception:
        logger.exception("Re-keying files for %r failed (will be fixed by the next rebuild)", word)


async def apply_candidate(bot, cand: dict):
    ctype = cand["type"]
    if ctype == "alias":
        await save_alias(cand["key"], cand["target"], cand["title"], by="ai-approved")
        # the same spelling may be waiting as a plain miss report in the channel
        from plugins.alias_fix import _edit, _done_text
        for other in await other_open_with_key(cand["key"], None):
            updated = await miss_transition(str(other["_id"]), ["open"], "done")
            if updated and updated.get("chat_msg_id") and NOT_FOUND_FILE_CHANNEL:
                await _edit(bot, NOT_FOUND_FILE_CHANNEL, updated["chat_msg_id"],
                            _done_text(updated, cand["title"], " (AI suggestion approved)"))
    elif ctype == "filterword":
        settings = await get_settings()
        words = list(settings["filter_words"])
        if cand["key"] not in [w.lower() for w in words]:
            await update_settings({"filter_words": words + [cand["key"]]})
    else:
        await L.add_noise(cand["key"])
        set_noise_words(set(get_noise_words()) | {cand["key"]})
        await _rekey(cand["key"])


async def revert_candidate(cand: dict):
    ctype = cand["type"]
    if ctype == "alias":
        await delete_alias(cand["key"])
    elif ctype == "filterword":
        settings = await get_settings()
        await update_settings({"filter_words": [w for w in settings["filter_words"] if w.lower() != cand["key"]]})
    else:
        await L.remove_noise(cand["key"])
        set_noise_words(set(get_noise_words()) - {cand["key"]})
        await _rekey(cand["key"])


# ── the buttons ─────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^ail#(ok|chg|no|undo)#"))
async def candidate_buttons(bot, query):
    if query.from_user.id not in ADMINS:
        await query.answer("Admins only.", show_alert=True)
        return
    _, action, cid = query.data.split("#", 2)
    handled = "Already handled by another admin."

    if action == "ok":
        cand = await L.cand_transition(cid, ["posted"], "approved")
        if not cand:
            return await query.answer(handled, show_alert=True)
        try:
            await apply_candidate(bot, cand)
        except Exception:
            logger.exception("Applying AI suggestion failed")
            await L.cand_transition(cid, ["approved"], "posted")
            return await query.answer("⚠️ Couldn't apply it. Try again.", show_alert=True)
        await _edit_cand_message(bot, cand, "approved")
        return await query.answer("Approved.")

    if action == "no":
        cand = await L.cand_transition(cid, ["posted"], "rejected")
        if not cand:
            return await query.answer(handled, show_alert=True)
        await L.block(cand["type"], cand["key"], cand["target"])
        await _edit_cand_message(bot, cand, "rejected")
        return await query.answer("Rejected — it won't be suggested again.")

    if action == "chg":
        # Hand over to the same "reply with the correct name" flow the Fix button uses.
        cand = await L.cand_transition(cid, ["posted"], "changing")
        if not cand:
            return await query.answer(handled, show_alert=True)
        await L.block("alias", cand["key"], cand["target"])
        text = (f"🧠 <b>AI suggestion · alias</b>\n<code>{html.escape(cand['key'])}</code> "
                f"(was: {html.escape(cand['title'])})")
        miss_id = await create_miss(cand.get("query") or cand["key"], cand["key"], 0, text)
        await set_miss_message(miss_id, query.message.id)
        await miss_transition(miss_id, ["open"], "waiting")
        await query.message.edit_text(
            text + "\n\n↩️ <b>Reply to this message with the correct name.</b>",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"nfa#can#{miss_id}")]]),
        )
        return await query.answer("Reply with the correct name.")

    # undo
    cand = await L.cand_transition(cid, ["approved"], "undone")
    if not cand:
        return await query.answer(handled, show_alert=True)
    try:
        await revert_candidate(cand)
    except Exception:
        logger.exception("Undoing AI suggestion failed")
    await L.block(cand["type"], cand["key"], cand["target"])
    await _edit_cand_message(bot, cand, "undone")
    if query.message.chat.type == enums.ChatType.PRIVATE:  # pressed from the AI Learning panel
        from plugins.learn_panel import render_recent
        text, markup = await render_recent()
        await query.message.edit_text(text, reply_markup=markup)
    await query.answer("Undone — it won't be suggested again.")
