"""
Typo-correction fallback for search.

Pipeline (each stage only runs if the previous one found nothing — a
correctly-spelled query never reaches any of this, so normal search speed
and accuracy are completely unaffected):

  Stage 1 (plugins/search.py)  — normal DB search.
  Stage 2 (fuzzy_correct)      — compares the query against a cache of
      titles already in your own database. Free, in-memory, no network
      call, a few milliseconds. Catches ordinary typos.
  Stage 3 (suggest_titles)     — TMDB and OMDb are searched first (every
      result they return is a real, catalogued title — zero hallucination
      risk); Groq and Gemini are only asked, in full, as a fallback if
      TMDB+OMDb together find nothing. Returns a list of candidate titles
      for the user to pick from as buttons — it never auto-applies one or
      shows a result directly. A click on a suggestion is what actually
      searches your database; if that title isn't in it, the user just
      sees "not found" for that pick.

fuzzy_correct() returns (title, results) — it's still an auto-apply stage,
unchanged. suggest_titles() returns a plain list of title strings.
"""
import asyncio
import logging
import re
import time

import aiohttp
from rapidfuzz.distance import DamerauLevenshtein

from config import (
    GROQ_API_KEY, GROQ_MODEL, GEMINI_API_KEY, GEMINI_MODEL,
    TMDB_API_KEY, OMDB_API_KEY, AI_FETCH_TIMEOUT, FUZZY_MATCH_THRESHOLD,
)
from database.filters_db import files, search_files, clean_title, display_name

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Stage 2 — fuzzy match against your own database's titles
# ══════════════════════════════════════════════════════════════════════════════

_TITLE_CACHE: list = []          # [(clean_lower_title, display_title), ...]
_TITLE_CACHE_TIME = 0.0
_TITLE_CACHE_TTL = 600            # rebuild at most every 10 minutes
_TITLE_CACHE_FETCH_LIMIT = 8000   # bounded — cheap even on a huge collection


async def _rebuild_title_cache() -> list:
    seen, result = set(), []
    cursor = files.find({}, {"file_name": 1, "caption": 1, "_id": 0}).limit(_TITLE_CACHE_FETCH_LIMIT)
    async for doc in cursor:
        clean = clean_title(display_name(doc))
        key = clean.lower()
        if key and key not in seen:
            seen.add(key)
            result.append((key, clean))
    return result


async def _get_title_cache() -> list:
    global _TITLE_CACHE, _TITLE_CACHE_TIME
    now = time.monotonic()
    if _TITLE_CACHE and (now - _TITLE_CACHE_TIME) < _TITLE_CACHE_TTL:
        return _TITLE_CACHE
    _TITLE_CACHE = await _rebuild_title_cache()
    _TITLE_CACHE_TIME = now
    return _TITLE_CACHE


def _word_count_ok(query_words: list, cand_words: list) -> bool:
    # Different word counts almost always mean a genuinely different title
    # ("Kaithi" vs "Naan Kaithi") rather than a typo of the same one — this
    # single guard is what stops the fuzzy stage from cross-matching
    # similar-looking but unrelated titles.
    return abs(len(cand_words) - len(query_words)) <= 1


def _per_word_score(query_words: list, cand_words: list) -> float:
    """
    Per-word similarity, averaged. Uses Damerau-Levenshtein similarity
    (substitutions and adjacent-letter transpositions both cost a single
    edit, same as insertions/deletions) rather than the Indel-style ratio
    most fuzzy libraries default to — Indel systematically scores a
    *dropped* letter ("boyz" -> "boy") higher than a *substituted* one
    ("boyz" -> "boys"), which is backwards for real typos and is exactly
    what let a shorter, unrelated title outscore the actual correct one.
    Damerau's transposition handling also catches the extremely common
    swapped-adjacent-letters typo ("pushap" -> "Pushpa") that plain
    Levenshtein undervalues. A small penalty proportional to leftover
    length difference then breaks any remaining tie in favour of the
    same-length (substitution-style) candidate — the more common real typo.
    """
    if not query_words or not cand_words:
        return 0.0
    total_score = 0.0
    total_len_diff = 0
    for qw in query_words:
        best_score, best_len_diff = 0.0, 0
        for cw in cand_words:
            if qw == cw:
                best_score, best_len_diff = 100.0, 0
                break
            if abs(len(qw) - len(cw)) > 2:
                continue
            score = DamerauLevenshtein.normalized_similarity(qw, cw) * 100
            if score > best_score:
                best_score, best_len_diff = score, abs(len(qw) - len(cw))
        total_score += best_score
        total_len_diff += best_len_diff
    n = len(query_words)
    return (total_score / n) - 0.5 * (total_len_diff / n)


def _trailing_number(text: str) -> str | None:
    m = re.search(r"\b(\d{1,3})\s*$", text.strip())
    return m.group(1) if m else None


def _sequel_number_preserved(original: str, candidate: str) -> bool:
    """
    A spelling corrector should never re-decide which sequel/season/part
    the user meant — only fix how they spelled it. If the query explicitly
    ends in a number ("Weak Hero Class 2"), a correction that changes or
    drops that number ("Weak Hero Class 1") is being changed to a
    different, real title, not spell-corrected — reject it outright rather
    than let a fuzzy score or an AI's best guess quietly substitute it.
    """
    original_num = _trailing_number(original)
    if original_num is None:
        return True  # nothing to protect
    return _trailing_number(candidate) == original_num


_SYMBOLS_RE = re.compile(r"[^\w\s]+")


def _best_fuzzy_match(query: str, cache: list):
    """Pure CPU work — called via asyncio.to_thread so it never blocks the
    event loop while other users' searches are being served."""
    # Symbols (: , ! ? & / – — ...) are word separators here, never part of a
    # word — "vvaan:" must compare as "vvaan".
    q_clean = _SYMBOLS_RE.sub(" ", clean_title(query).lower())
    q_words = q_clean.split()
    if not q_words:
        return None
    q_collapsed = "".join(q_words)

    best_score, best_title = 0.0, None
    for key, original in cache:
        if not _sequel_number_preserved(query, key):
            continue
        cand_words = _SYMBOLS_RE.sub(" ", key).split()
        if "".join(cand_words) == q_collapsed:
            return original  # same letters, just spacing/punctuation differs
        if not _word_count_ok(q_words, cand_words):
            continue
        score = _per_word_score(q_words, cand_words)
        if score > best_score:
            best_score, best_title = score, original

    return best_title if best_score >= FUZZY_MATCH_THRESHOLD else None


async def fuzzy_correct(query: str):
    """Returns (title, results) or None. Never returns a title without
    results that are actually, currently in the database — a stale cache
    entry (e.g. the file was deleted since the cache was last built) simply
    falls through to the AI stage instead of being trusted blindly."""
    cache = await _get_title_cache()
    if not cache:
        return None
    guess = await asyncio.to_thread(_best_fuzzy_match, query, cache)
    if not guess:
        return None
    results = await search_files(guess)
    return (guess, results) if results else None


# ══════════════════════════════════════════════════════════════════════════════
# Stage 3 — suggestion titles (TMDB -> OMDb -> AI list, in that order)
# Only reached when Stage 1 AND Stage 2 both found nothing. TMDB and OMDb
# are tried first because every result they return is a real, catalogued
# title — zero hallucination risk. Groq and Gemini (both, in full — not
# raced) are only asked if TMDB+OMDb together come back completely empty,
# as a safety net for obscure/regional titles neither catalog covers well.
# Nothing here ever touches your own database — the caller turns the
# returned titles into buttons, and only a click actually searches it.
# ══════════════════════════════════════════════════════════════════════════════

_MAX_SUGGESTIONS = 8


async def _tmdb_suggestions(query: str) -> list:
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
        logger.debug("TMDB suggestions failed", exc_info=True)
        return []
    titles = []
    for item in data.get("results", []):
        if item.get("media_type") not in ("movie", "tv"):
            continue
        title = item.get("title") or item.get("name")
        if title:
            titles.append(title)
    return titles


async def _omdb_suggestions(query: str) -> list:
    if not OMDB_API_KEY:
        return []
    params = {"apikey": OMDB_API_KEY, "s": query}
    timeout = aiohttp.ClientTimeout(total=AI_FETCH_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("http://www.omdbapi.com/", params=params) as r:
                if r.status != 200:
                    return []
                data = await r.json(content_type=None)
    except Exception:
        logger.debug("OMDb suggestions failed", exc_info=True)
        return []
    if data.get("Response") != "True":
        return []
    return [item["Title"] for item in data.get("Search", []) if item.get("Title")]


_AI_LIST_PROMPT = (
    "You suggest possible real movie/TV/anime titles for a misspelled or "
    "unclear search query on a file search engine. The user typed: "
    "\"{query}\". Reply with up to 5 real, existing titles this could be, "
    "one per line, nothing else — no numbering, no explanation, no year. "
    "Only fix spelling or minor wording — if the query names a specific "
    "numbered sequel, season, or part (e.g. ends in \"2\", \"Part 3\"), "
    "every title you suggest MUST keep that exact same number; never "
    "substitute a different entry in the series. If you cannot think of "
    "any real matching title, reply with exactly: NONE"
)


def _parse_title_lines(text: str) -> list:
    if text.strip().upper().strip(". ") == "NONE":
        return []
    titles = []
    for line in text.splitlines():
        line = line.strip().lstrip("-•*0123456789.) ").strip()
        if line and line.upper() != "NONE":
            titles.append(line)
    return titles


async def _groq_suggestions(query: str) -> list:
    if not GROQ_API_KEY:
        return []
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": _AI_LIST_PROMPT.format(query=query)}],
        "temperature": 0.3,
        "max_tokens": 120,
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    timeout = aiohttp.ClientTimeout(total=AI_FETCH_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload, headers=headers,
            ) as r:
                if r.status != 200:
                    return []
                data = await r.json()
        text = data["choices"][0]["message"]["content"].strip()
    except Exception:
        logger.debug("Groq suggestions failed", exc_info=True)
        return []
    return _parse_title_lines(text)


async def _gemini_suggestions(query: str) -> list:
    if not GEMINI_API_KEY:
        return []
    payload = {
        "contents": [{"parts": [{"text": _AI_LIST_PROMPT.format(query=query)}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 150},
    }
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    timeout = aiohttp.ClientTimeout(total=AI_FETCH_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as r:
                if r.status != 200:
                    return []
                data = await r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        logger.debug("Gemini suggestions failed", exc_info=True)
        return []
    return _parse_title_lines(text)


def _merge_titles(*title_lists) -> list:
    """Dedupe by cleaned title (so 'The Boy (2016)' and 'the boy' collapse
    into one), keeping the first-seen display form and preserving source
    priority — earlier lists in the arguments win the display spelling."""
    seen = set()
    merged = []
    for titles in title_lists:
        for title in titles:
            key = clean_title(title).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(title)
    return merged


def _same_series_wrong_number(query: str, candidate: str) -> bool:
    """
    True only when `candidate` is the SAME base title as `query` but with a
    different trailing sequel/season/part number — e.g. query "Weak Hero
    Class 2" vs candidate "Weak Hero Class 1". A completely different title
    that simply happens to also end in a digit ("Kaithi 2") is never
    touched by this — only an actual same-series mismatch is rejected.
    """
    q_num = _trailing_number(query)
    if q_num is None:
        return False
    q_base = re.sub(r"\s*\d{1,3}\s*$", "", clean_title(query)).lower()
    if not q_base:
        return False
    c_base = re.sub(r"\s*\d{1,3}\s*$", "", clean_title(candidate)).lower()
    c_num = _trailing_number(candidate)
    return c_base == q_base and c_num != q_num


async def suggest_titles(query: str) -> list:
    """
    Returns up to 8 candidate titles for the caller to present as buttons.
    Never returns more than one entry for the same underlying title, and
    never suggests the same series with a different sequel/season number
    than the one the query explicitly named.
    """
    tmdb, omdb = await asyncio.gather(_tmdb_suggestions(query), _omdb_suggestions(query))
    merged = _merge_titles(tmdb, omdb)

    if not merged:
        groq_list, gemini_list = await asyncio.gather(
            _groq_suggestions(query), _gemini_suggestions(query),
        )
        merged = _merge_titles(groq_list, gemini_list)

    merged = [t for t in merged if not _same_series_wrong_number(query, t)]
    return merged[:_MAX_SUGGESTIONS]
