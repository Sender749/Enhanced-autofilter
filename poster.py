"""
Automatic poster lookup for search results — zero admin setup required.

Cascade: TMDB (landscape backdrop preferred, portrait poster fallback) ->
OMDb (portrait only, IMDb data via REST API — never scrapes imdb.com, so it
never 403s). If neither has a confident match, no poster is attached to the
result page at all (never show a wrong/unrelated poster).

Search pages get their poster from the FILES that were actually found (see
fetch_poster_for_results): the title/year/series hint is read from the result
file names, never from whatever the user typed. When a search returns mixed
titles (e.g. "Hindi 2024"), the most common title in the results wins.

Results are cached in memory (per cleaned title+year) so re-searching the
same title, or paginating/filtering an existing search, never re-hits the
network.
"""
import re
import time
import asyncio
import logging
from collections import Counter

import aiohttp

from config import TMDB_API_KEY, OMDB_API_KEY, POSTER_FETCH_TIMEOUT
from database.filters_db import clean_title, display_name, _doc_text, _season_of, _year_of

logger = logging.getLogger(__name__)

TMDB_IMG_LANDSCAPE = "https://image.tmdb.org/t/p/w1280"
TMDB_IMG_PORTRAIT = "https://image.tmdb.org/t/p/w500"

_CACHE: dict = {}
_CACHE_TTL = 6 * 3600
_CACHE_MAX_ENTRIES = 500

# ── query cleanup — strip quality/lang/codec noise, pull out a year hint ────
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")
_SE_RE = re.compile(r"\bS\d{1,2}(E\d{1,3})?\b", re.IGNORECASE)
_NOISE_RE = re.compile(
    r"\b(240p|360p|480p|576p|720p|1080p|1440p|2160p|4k|uhd|hdr\d*|"
    r"hd[\-\s._]?(?:rip|tc|cam|ts)|hdrip|bluray|blu[\-\s]ray|bdrip|bd[\-\s]rip|brrip|remux|"
    r"web[\-\s._]?dl|web[\-\s._]?rip|hdtv|dvd[\-\s]?rip|dvd[\-\s]?scr|pre[\-\s]?dvd|"
    r"camrip|telesync|cam|hdts|ts|hd|"
    r"x264|x265|hevc|avc|aac|ac3|dts|ddp\d?\.?\d?|flac|mp3|"
    r"esub|esubs|subs?|subbed|dubbed|dual audio|multi audio|dual|multi|"
    r"hindi|english|tamil|telugu|kannada|malayalam|bengali|punjabi|marathi|"
    r"gujarati|urdu|korean|japanese)\b",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[._\-\+\[\]()]+")
_SPACE_RE = re.compile(r"\s{2,}")


def _clean_title_year(query: str) -> tuple[str, str]:
    text = query.strip()
    year_m = _YEAR_RE.search(text)
    year = year_m.group(0) if year_m else ""
    text = _SE_RE.sub(" ", text)
    text = _NOISE_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    if year:
        text = text.replace(year, " ")
    text = _SPACE_RE.sub(" ", text).strip()
    return text, year


def _cache_get(key: str):
    entry = _CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry[0] > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return entry[1]


def _cache_set(key: str, value):
    if len(_CACHE) >= _CACHE_MAX_ENTRIES:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (time.time(), value)


async def _tmdb_lookup(session: aiohttp.ClientSession, title: str, year: str, media_hint: str | None = None):
    params = {"api_key": TMDB_API_KEY, "query": title, "include_adult": "false"}
    if year:
        params["year"] = year
    async with session.get("https://api.themoviedb.org/3/search/multi", params=params) as r:
        if r.status != 200:
            return None
        data = await r.json()
    results = [x for x in data.get("results", []) if x.get("media_type") in ("movie", "tv")]
    if not results:
        return None

    title_lower = title.lower()
    exact = [
        x for x in results
        if (x.get("title") or x.get("name") or "").lower() == title_lower
    ]
    candidates = exact or results

    def _rank(x):
        # Prefer the entry whose release year matches the file's year, then
        # the one whose type (movie/tv) matches the file (S01E02 -> tv).
        # With no hints every candidate ties and the first (TMDB's own best
        # match) wins, exactly as before.
        date = x.get("release_date") or x.get("first_air_date") or ""
        year_ok = bool(year) and date.startswith(year)
        type_ok = (not media_hint) or x.get("media_type") == media_hint
        return (not year_ok, not type_ok)

    pick = min(candidates, key=_rank)
    matched = pick.get("title") or pick.get("name") or title

    backdrop = pick.get("backdrop_path")
    poster = pick.get("poster_path")
    if backdrop:
        return {"url": f"{TMDB_IMG_LANDSCAPE}{backdrop}", "landscape": True, "title": matched}
    if poster:
        return {"url": f"{TMDB_IMG_PORTRAIT}{poster}", "landscape": False, "title": matched}
    return None


async def _omdb_lookup(session: aiohttp.ClientSession, title: str, year: str, media_hint: str | None = None):
    params = {"apikey": OMDB_API_KEY, "t": title}
    if media_hint == "tv":
        # A file's year is the episode's air year, which rarely equals the
        # series' start year — so for series, match on type instead of year.
        params["type"] = "series"
    elif year:
        params["y"] = year
    async with session.get("http://www.omdbapi.com/", params=params) as r:
        if r.status != 200:
            return None
        data = await r.json(content_type=None)
    if data.get("Response") != "True":
        return None
    poster = data.get("Poster")
    if not poster or poster == "N/A":
        return None
    return {"url": poster, "landscape": False, "title": data.get("Title") or title}


async def _lookup(title: str, year: str, media_hint: str | None = None) -> dict | None:
    """Cached TMDB -> OMDb cascade for an already-cleaned title. Never raises."""
    if not TMDB_API_KEY and not OMDB_API_KEY:
        return None
    if not title:
        return None

    cache_key = f"{title.lower()}|{year}|{media_hint or ''}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached or None

    result = None
    timeout = aiohttp.ClientTimeout(total=POSTER_FETCH_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if TMDB_API_KEY:
                try:
                    result = await _tmdb_lookup(session, title, year, media_hint)
                except Exception:
                    logger.debug("TMDB lookup failed for %r", title, exc_info=True)
            if not result and OMDB_API_KEY:
                try:
                    result = await _omdb_lookup(session, title, year, media_hint)
                except Exception:
                    logger.debug("OMDb lookup failed for %r", title, exc_info=True)
    except (asyncio.TimeoutError, aiohttp.ClientError):
        logger.debug("Poster fetch network error for %r", title)

    _cache_set(cache_key, result)
    return result


async def fetch_poster(query: str) -> dict | None:
    """
    Poster for a free-text title (used by movie-update posts). Returns
    {"url": str, "landscape": bool, "title": str} or None if no confident
    match was found anywhere. Never raises — a poster-provider hiccup should
    never break anything.
    """
    title, year = _clean_title_year(query)
    return await _lookup(title, year)


def _title_of_doc(doc: dict) -> str:
    title = clean_title(display_name(doc))
    if not title:  # caption was nothing but symbols — fall back to the file name
        title = clean_title(doc.get("file_name", "") or "")
    return title


def _pick_title_from_results(results: list):
    """-> (title, year, media_hint) read from the result files themselves.
    Picks the most common title in the set (first one on a tie), then the
    most common year and series/movie hint among that title's files."""
    titled = [(_title_of_doc(doc), doc) for doc in results]
    titled = [(t, d) for t, d in titled if t]
    if not titled:
        return "", "", None

    winner = Counter(t.lower() for t, _ in titled).most_common(1)[0][0]
    docs = [d for t, d in titled if t.lower() == winner]
    title = next(t for t, _ in titled if t.lower() == winner)

    years = Counter(y for y in (_year_of(_doc_text(d)) for d in docs) if y)
    year = years.most_common(1)[0][0] if years else ""
    is_series = any(_season_of(_doc_text(d)) for d in docs)
    return title, year, ("tv" if is_series else None)


async def fetch_poster_for_results(results: list) -> dict | None:
    """Poster for a search page, based on the files that were found (never on
    the user's typed query). No match -> None, so no poster is sent."""
    if not results:
        return None
    title, year, media_hint = _pick_title_from_results(results)
    return await _lookup(title, year, media_hint)
