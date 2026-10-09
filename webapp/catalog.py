"""
Web app catalog. Two extra collections, the main files collection is only read:

  webapp_items   one doc per file  (_id = the file's own _id): parsed season /
                 episode range / pack / quality / languages, linked to a title.
  webapp_titles  one doc per title (_id = stable title key): name, kind, poster
                 and metadata from TMDB, counters and sort timestamps.

`sync()` is incremental (only files newer than the last run) and idempotent, so
it is safe to run on a timer. `enrich_loop()` fills in posters in the
background at a polite rate; titles without a confident TMDB match keep
meta_status="none" and the app renders a styled fallback card for them.
"""
import asyncio
import logging
import re
import time

import aiohttp
from pymongo import ASCENDING, DESCENDING, ReplaceOne, UpdateOne
from rapidfuzz import fuzz

from config import TMDB_API_KEY
from database.client import db
from database.filters_db import files, display_name
from webapp.parser import parse, key as title_key, category

logger = logging.getLogger(__name__)

items = db["webapp_items"]
titles = db["webapp_titles"]
state = db["webapp_state"]
lists = db["webapp_lists"]

_WORD = re.compile(r"[a-z0-9]+")
_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary",
    18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
    9648: "Mystery", 10749: "Romance", 878: "Sci-Fi", 10770: "TV Movie", 53: "Thriller",
    10752: "War", 37: "Western", 10759: "Action & Adventure", 10762: "Kids", 10763: "News",
    10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics",
}

_sync_lock = asyncio.Lock()


def tokens(text: str) -> list:
    return _WORD.findall((text or "").lower())


async def ensure_indexes():
    await items.create_index([("tkey", ASCENDING), ("season", ASCENDING)], name="item_title_idx")
    await titles.create_index([("last_indexed_at", DESCENDING)], name="t_recent_idx")
    await titles.create_index([("release_ts", DESCENDING)], name="t_release_idx")
    await titles.create_index([("words", ASCENDING)], name="t_words_idx")
    await titles.create_index([("kind", ASCENDING), ("last_indexed_at", DESCENDING)], name="t_kind_idx")
    await titles.create_index([("meta_status", ASCENDING), ("last_indexed_at", DESCENDING)], name="t_meta_idx")


# ── sync: files -> items + titles ───────────────────────────────────────────

def _year_ts(year) -> float | None:
    try:
        return time.mktime((int(year), 1, 1, 0, 0, 0, 0, 0, 0))
    except Exception:
        return None


async def _refresh_titles(tkeys: list):
    """Recompute counters for the given titles from their items."""
    pipeline = [
        {"$match": {"tkey": {"$in": tkeys}}},
        {"$group": {
            "_id": "$tkey", "n": {"$sum": 1}, "name": {"$first": "$name"}, "year": {"$max": "$year"},
            "series": {"$sum": {"$cond": ["$is_series", 1, 0]}}, "anime": {"$max": "$anime"},
            "seasons": {"$addToSet": "$season"}, "q": {"$addToSet": "$quality"},
            "langs": {"$push": "$langs"},
            "first": {"$min": "$indexed_at"}, "last": {"$max": "$indexed_at"},
        }},
    ]
    ops = []
    anime_meta = {t["_id"] async for t in titles.find({"_id": {"$in": tkeys}, "anime_meta": True}, {"_id": 1})}
    async for g in items.aggregate(pipeline):
        langs = sorted({l for ls in g["langs"] for l in (ls or [])})
        is_series = g["series"] * 2 > g["n"]
        kind = "anime" if ((g.get("anime") or g["_id"] in anime_meta) and is_series) else ("series" if is_series else "movie")
        qs = sorted({q for q in g["q"] if q}, key=lambda x: -int(x[:-1]))
        seasons = sorted(s for s in g["seasons"] if s is not None)
        set_doc = {
            "kind": kind, "file_count": g["n"], "seasons": seasons, "langs": langs, "qualities": qs,
            "first_indexed_at": g["first"], "last_indexed_at": g["last"],
        }
        on_insert = {"name": g["name"], "year": g["year"], "words": sorted(set(tokens(g["name"]))),
                     "meta_status": "pending", "release_ts": _year_ts(g["year"]), "rating": None}
        ops.append(UpdateOne({"_id": g["_id"]}, {"$set": set_doc, "$setOnInsert": on_insert}, upsert=True))
    if ops:
        await titles.bulk_write(ops, ordered=False)


async def sync(full: bool = False, batch: int = 1000, rematch: bool = False) -> int:
    """Parse files newer than the last sync. Returns files processed.
    full=True re-parses everything; rematch=True also re-checks every poster/metadata match."""
    if _sync_lock.locked():
        return 0
    async with _sync_lock:
        if rematch:
            await titles.update_many({}, {"$set": {"rematch": True}})
        st = await state.find_one({"_id": "sync"}) or {}
        last = 0.0 if full else float(st.get("last_ts") or 0.0)
        cursor = files.find({"indexed_at": {"$gt": last}}, {
            "caption": 1, "file_name": 1, "file_size": 1, "mime_type": 1, "file_type": 1, "indexed_at": 1,
        }).sort("indexed_at", ASCENDING)
        total, ops, touched, newest = 0, [], set(), last

        async def flush():
            nonlocal ops, touched
            if ops:
                await items.bulk_write(ops, ordered=False)
                await _refresh_titles(list(touched))
            ops, touched = [], set()

        async for doc in cursor:
            name = display_name(doc)
            p = parse(name)
            k = title_key(p)
            newest = max(newest, doc.get("indexed_at") or 0)
            if not k or "junk_or_empty_caption" in p["flags"]:
                continue
            ops.append(ReplaceOne({"_id": doc["_id"]}, {
                "tkey": k, "name": p["title"], "year": p["year"], "is_series": p["is_series"], "anime": p["anime"],
                "season": p["season"], "ep_from": p["ep_from"], "ep_to": p["ep_to"], "pack": p["pack"],
                "bonus": p["bonus"], "quality": p["quality"], "langs": p["langs"],
                "size": doc.get("file_size") or 0, "label": name[:200],
                "mime": doc.get("mime_type") or "", "indexed_at": doc.get("indexed_at") or 0,
            }, upsert=True))
            touched.add(k)
            total += 1
            if len(ops) >= batch:
                await flush()
                await asyncio.sleep(0)
        await flush()
        await state.update_one({"_id": "sync"}, {"$set": {"last_ts": newest, "ran_at": time.time()}}, upsert=True)
        if total:
            logger.info("Web app catalog sync: %s files processed.", total)
        return total


# ── TMDB enrichment ─────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return " ".join(tokens(s))


async def _tmdb_match(session: aiohttp.ClientSession, name: str, year: str | None, kind: str):
    params = {"api_key": TMDB_API_KEY, "query": name, "include_adult": "false"}
    if year and kind == "movie":
        params["year"] = year
    async with session.get("https://api.themoviedb.org/3/search/multi", params=params) as r:
        if r.status != 200:
            return "error"
        data = await r.json()
    results = [x for x in data.get("results", []) if x.get("media_type") in ("movie", "tv")]
    want = "movie" if kind == "movie" else "tv"
    best, best_score = None, 0
    for x in results[:8]:
        cand = x.get("title") or x.get("name") or ""
        score = fuzz.ratio(_norm(name), _norm(cand))
        date = x.get("release_date") or x.get("first_air_date") or ""
        if x.get("media_type") != want:
            score -= 12
        if year and date.startswith(str(year)):
            score += 8
        cy = int(date[:4]) if date[:4].isdigit() else None
        if year and str(year).isdigit() and cy:  # reject remakes / same-name titles from the wrong year
            y = int(year)
            if want == "movie" and abs(cy - y) > 1:
                score -= 30
            elif want == "tv" and cy > y + 1:
                score -= 30
        if score > best_score:
            best, best_score = x, score
    if not best or best_score < 80 or not (best.get("poster_path") or best.get("backdrop_path")):
        return None
    return best


async def _enrich_one(session, t: dict):
    try:
        m = await _tmdb_match(session, t["name"], t.get("year"), t.get("kind", "movie"))
    except Exception:
        logger.warning("TMDB lookup failed for %r", t["name"], exc_info=True)
        return False
    if m == "error":
        return False
    if m is None:
        await titles.update_one({"_id": t["_id"]}, {"$set": {
            "meta_status": "none", "meta_at": time.time(), "rematch": False,
            "poster": None, "backdrop": None, "overview": None, "rating": None}})
        return True
    date = m.get("release_date") or m.get("first_air_date") or ""
    try:
        release_ts = time.mktime(time.strptime(date, "%Y-%m-%d"))
    except Exception:
        release_ts = None
    is_tv = m.get("media_type") == "tv"
    if is_tv and t.get("year"):
        # A series' TMDB date is its first season; the caption year tracks the newest season/episodes.
        ys = _year_ts(t["year"])
        if ys and (release_ts is None or ys > release_ts):
            release_ts = ys
    genres = [_GENRES[g] for g in (m.get("genre_ids") or []) if g in _GENRES][:3]
    upd = {
        "meta_status": "ok", "meta_at": time.time(), "rematch": False, "tmdb_id": m.get("id"),
        "poster": m.get("poster_path"), "backdrop": m.get("backdrop_path"),
        "overview": (m.get("overview") or "")[:600], "rating": round(m.get("vote_average") or 0, 1),
        "genres": genres, "release_date": date or None,
        "display_name": m.get("title") or m.get("name") or t["name"],
    }
    if release_ts:
        upd["release_ts"] = release_ts
    if is_tv and 16 in (m.get("genre_ids") or []) and m.get("original_language") == "ja":
        upd["kind"] = "anime"
        upd["anime_meta"] = True
    await titles.update_one({"_id": t["_id"]}, {"$set": upd})
    return True


async def enrich_once(limit: int = 40, parallel: int = 8) -> int:
    """Look up posters for pending titles, `parallel` at a time (TMDB allows far more)."""
    cur = titles.find({"$or": [{"meta_status": "pending"}, {"rematch": True}]}, {"name": 1, "year": 1, "kind": 1}) \
        .sort("last_indexed_at", DESCENDING).limit(limit)
    todo = [t async for t in cur]
    if not todo:
        return 0
    failed = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        for i in range(0, len(todo), parallel):
            res = await asyncio.gather(*(_enrich_one(session, t) for t in todo[i:i + parallel]))
            failed += res.count(False)
            await asyncio.sleep(0.3 if not failed else 3)
    return len(todo) if failed < len(todo) else 0


async def background_loop(interval: int):
    """Sync new files, then enrich pending titles until the queue is empty."""
    await asyncio.sleep(15)
    while True:
        try:
            await sync()
            while await enrich_once():
                pass
        except Exception:
            logger.exception("Web app catalog loop error")
        await asyncio.sleep(interval)
