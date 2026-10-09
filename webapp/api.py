"""
Web app HTTP API (aiohttp). Every /api route needs a valid Telegram `initData`
in the X-Init-Data header, so the app only works when opened from the bot.

Delivery reuses the bot's own machinery, nothing is re-implemented:
  - "Send to bot PM"  -> plugins.deliver.deliver_file (all gates apply: premium,
                         force-sub, daily limit, verification)
  - Stream / Download -> same BIN_CHANNEL copy + signed link + host choice +
                         daily quota as the bot's Fast Download button
"""
import asyncio
import logging
import re
import time
from types import SimpleNamespace

from aiohttp import web
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import DESCENDING, ASCENDING
from rapidfuzz import process, fuzz

from config import (
    BIN_CHANNEL, FASTDL_ENABLED, STREAM_DAILY_LIMIT, STREAM_PREMIUM_DAILY_LIMIT,
)
from database.filters_db import get_file_by_id, display_name
from database.premium_db import has_premium_access
from database.request_db import add_request
from database.stream_db import get_bin_entry, add_usage, get_daily, increment_daily
from database import trending_db
from fastdl.hosts import choose_host
from fastdl.links import build_url, is_video
from utils import temp
from webapp.auth import validate_init_data
from webapp.catalog import titles, items, lists, tokens
from webapp.page import HTML
from webapp.parser import print_rank

logger = logging.getLogger(__name__)

NEW_DAYS = 3
_cache: dict = {}
_names: dict = {"at": 0, "ids": [], "norm": []}
_links: dict = {}  # (user, file) -> (time, payload)  so re-taps don't burn quota


# ── helpers ──────────────────────────────────────────────────────────────────

def _json(data, status=200):
    resp = web.json_response(data, status=status, headers={"Cache-Control": "private, no-store"})
    resp.enable_compression()
    return resp


def _user(request):
    return validate_init_data(request.headers.get("X-Init-Data", ""))


def _card(t: dict) -> dict:
    return {
        "id": t["_id"], "name": t.get("display_name") or t.get("name") or "", "year": t.get("year"),
        "kind": t.get("kind"), "poster": t.get("poster"), "backdrop": t.get("backdrop"),
        "rating": t.get("rating") or None, "quality": (t.get("qualities") or [None])[0],
        "langs": (t.get("langs") or [])[:2],
        "new": (time.time() - (t.get("last_indexed_at") or 0)) < NEW_DAYS * 86400,
        "seasons": len(t.get("seasons") or []),
    }


_FIELDS = {"name": 1, "display_name": 1, "year": 1, "kind": 1, "poster": 1, "backdrop": 1, "rating": 1,
           "qualities": 1, "langs": 1, "last_indexed_at": 1, "seasons": 1, "overview": 1, "genres": 1}


async def _rows(query: dict, sort: str, limit: int = 20, skip: int = 0, direction=DESCENDING) -> list:
    cur = titles.find(query, _FIELDS).sort(sort, direction).skip(skip).limit(limit)
    return [_card(t) async for t in cur]


def _categories() -> dict:
    """key -> (title, mongo query, sort field). Home rows and their "View all" pages share these,
    so a row and its full list can never disagree."""
    return {
        "recent": ("Recently added", {}, "last_indexed_at"),
        "released": ("New releases", {"release_ts": {"$lte": time.time()}, "meta_status": "ok"}, "release_ts"),
        "movies": ("Movies", {"kind": "movie"}, "last_indexed_at"),
        "series": ("Web series", {"kind": "series"}, "last_indexed_at"),
        "anime": ("Anime", {"kind": "anime"}, "last_indexed_at"),
        "hindi": ("Hindi", {"langs": "hindi"}, "last_indexed_at"),
        "punjabi": ("Punjabi", {"langs": "punjabi"}, "last_indexed_at"),
    }


_refreshing: set = set()


async def _cached(key: str, ttl: int, fn):
    """Stale-while-revalidate: a cached value is returned instantly even when it's old,
    and a background refresh is started, so users never wait for the database after the first load."""
    hit = _cache.get(key)
    now = time.time()
    if hit:
        if now - hit[0] > ttl and key not in _refreshing:
            _refreshing.add(key)

            async def _refresh():
                try:
                    _cache[key] = (time.time(), await fn())
                except Exception:
                    logger.warning("cache refresh failed for %s", key, exc_info=True)
                finally:
                    _refreshing.discard(key)
            asyncio.create_task(_refresh())
        return hit[1]
    val = await fn()
    _cache[key] = (time.time(), val)
    return val


async def warm_loop():
    """Keeps the home page ready so the app opens instantly."""
    await asyncio.sleep(20)
    while True:
        try:
            _cache["home"] = (time.time(), await _home_data())
        except Exception:
            logger.warning("home warm-up failed", exc_info=True)
        await asyncio.sleep(90)


# ── routes ───────────────────────────────────────────────────────────────────

async def page(_request):
    resp = web.Response(text=HTML, content_type="text/html", headers={"Cache-Control": "no-cache"})
    resp.enable_compression()
    return resp


async def _home_data():
    cats = _categories()
    hero_q = titles.find({"meta_status": "ok", "backdrop": {"$ne": None}}, _FIELDS).sort("last_indexed_at", DESCENDING).limit(6)

    async def hero_rows():
        return [{**_card(t), "overview": t.get("overview"), "genres": t.get("genres")} async for t in hero_q]

    keys = list(cats)
    results = await asyncio.gather(
        hero_rows(), _cached("trending", 600, _trending),
        *(_rows(cats[k][1], cats[k][2]) for k in keys))
    hero, trending, rows = results[0], results[1], dict(zip(keys, results[2:]))
    order = ["recent", "released", "trending", "movies", "series", "anime", "hindi", "punjabi"]
    out = []
    for k in order:
        if k == "trending":
            out.append({"key": k, "title": "Trending", "items": trending})
        else:
            out.append({"key": k, "title": cats[k][0], "items": rows[k]})
    return {"hero": hero, "rows": [r for r in out if r["items"]]}


async def _trending() -> list:
    docs, _ = await trending_db.get_page(0, 14)

    async def one(d):
        toks = tokens(d.get("name", ""))
        if not toks:
            return None
        return await titles.find_one({"words": {"$all": toks[:4]}}, _FIELDS, sort=[("last_indexed_at", -1)])

    found = await asyncio.gather(*(one(d) for d in docs))
    out, seen = [], set()
    for t in found:
        if t and t["_id"] not in seen:
            seen.add(t["_id"])
            out.append(_card(t))
    return out


async def category(request):
    """Full list behind a home row's "View all" button."""
    if not _user(request):
        return _json({"error": "auth"}, 401)
    key = request.match_info["key"]
    page_no = max(0, int(request.query.get("page", "0") or 0))
    size = 30
    if key == "trending":
        items_ = await _cached("trending", 600, _trending)
        return _json({"title": "Trending", "items": items_, "more": False})
    cat = _categories().get(key)
    if not cat:
        return _json({"error": "not_found"}, 404)
    cards = await _rows(cat[1], cat[2], size, page_no * size)
    return _json({"title": cat[0], "items": cards, "more": len(cards) == size})


async def home(request):
    if not _user(request):
        return _json({"error": "auth"}, 401)
    return _json(await _cached("home", 90, _home_data))


async def _load_names():
    if time.time() - _names["at"] < 600 and _names["ids"]:
        return
    ids, norm = [], []
    async for t in titles.find({}, {"name": 1, "display_name": 1}):
        ids.append(t["_id"])
        norm.append(" ".join(tokens(t.get("display_name") or t["name"])))
    _names.update(at=time.time(), ids=ids, norm=norm)


async def search(request):
    if not _user(request):
        return _json({"error": "auth"}, 401)
    q = request.query.get("q", "").strip()
    kind, lang = request.query.get("type"), request.query.get("lang")
    year, quality = request.query.get("year"), request.query.get("quality")
    sort = request.query.get("sort", "latest")
    page_no = max(0, int(request.query.get("page", "0") or 0))
    size = 30

    flt: dict = {}
    if kind in ("movie", "series", "anime"):
        flt["kind"] = kind
    if lang:
        flt["langs"] = lang.lower()
    if year and year.isdigit():
        flt["year"] = year
    if quality:
        flt["qualities"] = quality
    sort_field, direction = {
        "latest": ("last_indexed_at", DESCENDING), "release": ("release_ts", DESCENDING),
        "rating": ("rating", DESCENDING), "az": ("name", ASCENDING),
    }.get(sort, ("last_indexed_at", DESCENDING))

    toks = tokens(q)
    if toks:
        conds = [{"words": t} for t in toks[:-1]] + [{"words": {"$regex": "^" + re.escape(toks[-1])}}]
        exact = {**flt, "$and": conds}
        cards = await _rows(exact, sort_field, size, page_no * size, direction)
        if not cards and page_no == 0:  # typo tolerance: fuzzy match on title names
            await _load_names()
            hits = process.extract(" ".join(toks), _names["norm"], scorer=fuzz.WRatio, score_cutoff=72, limit=30)
            ids = [_names["ids"][i] for _, _, i in hits]
            if ids:
                found = {t["_id"]: t async for t in titles.find({**flt, "_id": {"$in": ids}})}
                cards = [_card(found[i]) for i in ids if i in found]
        return _json({"items": cards, "page": page_no, "more": len(cards) == size})
    cards = await _rows(flt, sort_field, size, page_no * size, direction)
    return _json({"items": cards, "page": page_no, "more": len(cards) == size})


def _file(it: dict) -> dict:
    return {
        "id": str(it["_id"]), "quality": it.get("quality"), "langs": it.get("langs") or [],
        "size": it.get("size") or 0, "label": it.get("label") or "",
        "video": (it.get("mime") or "").startswith("video/") or (it.get("label") or "").lower().endswith((".mkv", ".mp4", ".webm")),
    }


def _qkey(f: dict) -> int:
    try:
        return -int((f.get("quality") or "0p")[:-1])
    except ValueError:
        return 0


def _best(its: list) -> list:
    """Good prints only, one file per quality.
    1) keep only the best print tier present (a WEB-DL/BluRay title hides its HDRip/CAM copies;
       if only CAM-type prints exist they are all kept so the title isn't empty);
    2) per quality (480p, 720p, 1080p ...) keep one file: best print, then more audio languages, then larger."""
    if not its:
        return []
    ranked = [(print_rank(i.get("label", "")), i) for i in its]
    top = max(r for r, _ in ranked)
    keep = [(r, i) for r, i in ranked if top <= 30 or r >= top - 15]
    by_q: dict = {}
    for r, i in keep:
        score = (r, len(i.get("langs") or []), i.get("size") or 0)
        k = i.get("quality") or "?"
        if k not in by_q or score > by_q[k][0]:
            by_q[k] = (score, i)
    return sorted((_file(i) for _, i in by_q.values()), key=_qkey)


async def title(request):
    if not _user(request):
        return _json({"error": "auth"}, 401)
    t = await titles.find_one({"_id": request.match_info["id"]})
    if not t:
        return _json({"error": "not_found"}, 404)
    out = {**_card(t), "overview": t.get("overview"), "genres": t.get("genres"),
           "release_date": t.get("release_date"), "seasons_list": t.get("seasons") or [],
           "file_count": t.get("file_count")}
    if t.get("kind") == "movie":
        out["files"] = _best([i async for i in items.find({"tkey": t["_id"]}).limit(300)])
    else:
        # Seasons with no season number (episode-only captions) use key 0.
        seen = set(await items.distinct("season", {"tkey": t["_id"]}))
        out["seasons_list"] = sorted(0 if s is None else s for s in seen)
    return _json(out)


async def season(request):
    """One season: single episodes first; combined-episode files and full-season packs are
    plain rows listed below them (no extra tags). Each list shows good prints, one per quality."""
    if not _user(request):
        return _json({"error": "auth"}, 401)
    tid, n = request.match_info["id"], int(request.match_info["n"])
    q = {"tkey": tid, "season": None if n == 0 else n}
    singles: dict = {}
    combined: dict = {}
    packs, extras = [], []
    async for it in items.find(q).limit(2500):
        if it.get("bonus"):
            extras.append(it)
        elif it.get("ep_from") is not None:
            if it["ep_to"] > it["ep_from"]:
                combined.setdefault((it["ep_from"], it["ep_to"]), []).append(it)
            else:
                singles.setdefault(it["ep_from"], []).append(it)
        elif it.get("pack"):
            packs.append(it)
        else:
            extras.append(it)
    episodes = [{"ep": e, "files": _best(fs)} for e, fs in sorted(singles.items())]
    comb_files = [f for rng in sorted(combined) for f in _best(combined[rng])]
    return _json({"season": n, "episodes": episodes, "combined": comb_files,
                  "packs": _best(packs), "extras": _best(extras)})


async def link(request):
    """Stream + download URLs for one file (counts once against the user's daily quota)."""
    user = _user(request)
    if not user:
        return _json({"error": "auth"}, 401)
    if not FASTDL_ENABLED:
        return _json({"error": "Streaming isn't available right now."}, 503)
    body = await request.json()
    file_id = str(body.get("file", ""))
    key = (user["id"], file_id)
    hit = _links.get(key)
    if hit and time.time() - hit[0] < 600:
        return _json(hit[1])
    limit = STREAM_PREMIUM_DAILY_LIMIT if await has_premium_access(user["id"]) else STREAM_DAILY_LIMIT
    if await get_daily(user["id"]) >= limit:
        return _json({"error": f"Daily link limit reached ({limit}). Try again tomorrow."}, 429)
    doc = await get_file_by_id(file_id)
    if not doc:
        return _json({"error": "File not found."}, 404)

    from plugins.fast_download import _copy_is_alive, _copy_to_bin, _log_user_in_bin
    bot = temp.BOT
    try:
        entry = await get_bin_entry(file_id)
        if not entry or not await _copy_is_alive(bot, entry):
            entry = await _copy_to_bin(bot, file_id, doc, replace=bool(entry))
        size = int(entry.get("size") or 0)
        host = await choose_host(size)
        if not host:
            return _json({"error": "No stream server is available right now."}, 503)
        name = entry.get("name") or "file"
        dl = build_url(host.base_url, entry["bin_msg_id"], user["id"], name)
        token = dl.split("/dl/", 1)[1].split("/", 1)[0]
        qname = dl.rsplit("/", 1)[1]
        video = is_video(name, doc.get("mime_type", ""))
        payload = {
            "download": dl, "name": name, "video": video,
            "stream": f"{host.base_url}/stream/{token}/{qname}" if video else None,
            "watch": f"{host.base_url}/watch/{token}/{qname}" if video else None,
        }
        await add_usage(host.key, size)
        await increment_daily(user["id"])
        u = SimpleNamespace(id=user["id"], first_name=user.get("first_name", ""), last_name=user.get("last_name", ""),
                            username=user.get("username"))
        asyncio.create_task(_log_user_in_bin(bot, entry, doc, u))
        _links[key] = (time.time(), payload)
        return _json(payload)
    except Exception:
        logger.exception("webapp link failed for %s", file_id)
        return _json({"error": "Couldn't create the link. Try again."}, 500)


async def send_dm(request):
    user = _user(request)
    if not user:
        return _json({"error": "auth"}, 401)
    file_id = str((await request.json()).get("file", ""))
    try:
        ObjectId(file_id)
    except InvalidId:
        return _json({"error": "bad file"}, 400)
    from plugins.deliver import deliver_file
    task = asyncio.create_task(deliver_file(temp.BOT, user["id"], file_id))
    done, _ = await asyncio.wait({task}, timeout=4)  # usually delivered within a second or two
    if task in done and task.exception():
        logger.warning("webapp send failed: %s", task.exception())
        return _json({"error": "Couldn't send the file. Open the bot and press Start, then try again."}, 500)
    return _json({"ok": True, "bot": temp.U_NAME})


async def mylist(request):
    user = _user(request)
    if not user:
        return _json({"error": "auth"}, 401)
    if request.method == "POST":
        b = await request.json()
        op = {"$addToSet": {"ids": b["id"]}} if b.get("on") else {"$pull": {"ids": b["id"]}}
        await lists.update_one({"_id": user["id"]}, op, upsert=True)
    doc = await lists.find_one({"_id": user["id"]}) or {}
    ids = doc.get("ids", [])
    if request.query.get("full"):
        found = {t["_id"]: t async for t in titles.find({"_id": {"$in": ids}})}
        return _json({"ids": ids, "items": [_card(found[i]) for i in reversed(ids) if i in found]})
    return _json({"ids": ids})


async def request_title(request):
    user = _user(request)
    if not user:
        return _json({"error": "auth"}, 401)
    text = str((await request.json()).get("text", "")).strip()[:120]
    if not text:
        return _json({"error": "empty"}, 400)
    await add_request(user["id"], text, user.get("username"))
    return _json({"ok": True})


async def config(request):
    if not _user(request):
        return _json({"error": "auth"}, 401)
    return _json({"stream": bool(FASTDL_ENABLED), "bot": temp.U_NAME})


def register_routes(app: web.Application):
    app.add_routes([
        web.get("/app", page), web.get("/app/", page),
        web.get("/api/config", config), web.get("/api/home", home), web.get("/api/category/{key}", category), web.get("/api/search", search),
        web.get("/api/title/{id}", title), web.get("/api/title/{id}/season/{n}", season),
        web.post("/api/link", link), web.post("/api/send", send_dm),
        web.get("/api/mylist", mylist), web.post("/api/mylist", mylist),
        web.post("/api/request", request_title),
    ])
