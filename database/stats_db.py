"""
Numbers behind the /stats panel.

The bot already stores some of this (users, files, search log, requests). What it
did NOT store is *per day* activity, so this module records it:

  stat_daily   one tiny document per IST day, kept forever
               {_id: "2026-10-29", msgs, pm, grp, files, h: {"21": n, ...} (messages per hour),
                fh: {...} (files per hour), vp1/vp2/vp3 (shortener prompts shown),
                vd1/vd2/vd3 (shortener completions)}
  stat_users   one document per active user per day  {_id: "day|uid", day, uid, nm, un, m (messages),
               f (files received), fn (last 5 file names), pm / g (used PM / a group), at, last}
  stat_groups  one document per active group per day {_id: "day|gid", day, gid, m, at}
  stat_verify  one document per shortener completion {_id: "day|uid|tier", day, uid, tier, domain, ts}

The per-user / per-group / per-completion collections delete themselves after
STATS_KEEP_DAYS (default 62) so the database doesn't fill up; the daily totals stay.

Writes are collected in memory and saved in bulk every few seconds, so recording
never slows a search or a file delivery down, and every recorder is safe to call
(it never raises).
"""
import asyncio
import logging
import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from pymongo import ASCENDING, UpdateOne

from config import ADMINS
from database.client import db
from database.filters_db import files
from database.query_log_db import queries
from utils import IST

logger = logging.getLogger(__name__)

daily_col = db["stat_daily"]
users_col = db["stat_users"]
groups_col = db["stat_groups"]
verify_col = db["stat_verify"]

KEEP_DAYS = int(os.environ.get("STATS_KEEP_DAYS", "62"))
FLUSH_EVERY = 15      # seconds

# Search outcomes (see database/query_log_db.py)
HIT_OUTCOMES = ("found", "fuzzy", "relaxed", "titles")
MISS_OUTCOMES = ("related", "suggested", "none")


# ══════════════════════════════════════════════════════════════════════════════
# DAYS
# ══════════════════════════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(timezone.utc)


def ist_day(offset: int = 0, at: datetime | None = None) -> str:
    """IST calendar day as 'YYYY-MM-DD'. offset=-1 is yesterday."""
    base = (at or _now()).astimezone(IST) + timedelta(days=offset)
    return base.strftime("%Y-%m-%d")


def days_back(n: int, end_offset: int = 0) -> list:
    """The last n IST days (oldest first), ending today (or end_offset days from today)."""
    return [ist_day(end_offset - i) for i in range(n - 1, -1, -1)]


def day_bounds(day: str) -> tuple:
    """UTC start / end instants of an IST day."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def _range(days: list) -> dict:
    return {"$gte": days[0], "$lte": days[-1]}


# ══════════════════════════════════════════════════════════════════════════════
# RECORDING (in-memory, flushed in bulk)
# ══════════════════════════════════════════════════════════════════════════════

_u: dict = {}                 # (day, uid) -> counters
_g: dict = {}                 # (day, gid) -> messages
_d: dict = {}                 # day -> Counter
_flusher: asyncio.Task | None = None
_indexes_ready = False


def _kick():
    global _flusher
    if _flusher is None or _flusher.done():
        try:
            _flusher = asyncio.get_running_loop().create_task(_flush_loop())
        except RuntimeError:          # no running loop (tests / import time)
            pass


def _entry(day: str, uid: int) -> dict:
    e = _u.get((day, uid))
    if e is None:
        e = _u[(day, uid)] = {"m": 0, "f": 0, "fn": [], "pm": False, "g": False, "nm": None, "un": None, "at": _now()}
    return e


def record_activity(uid: int, name: str | None, username: str | None, chat_id: int | None, in_group: bool):
    """A message / button press by a real user. `chat_id` is the group when in_group."""
    try:
        if uid in ADMINS:                 # admins testing the bot shouldn't inflate the numbers
            return
        now = _now()
        day, hour = ist_day(at=now), now.astimezone(IST).strftime("%H")
        e = _entry(day, uid)
        e["m"] += 1
        e["pm" if not in_group else "g"] = True
        if name:
            e["nm"], e["un"] = name[:60], username
        d = _d.setdefault(day, Counter())
        d["msgs"] += 1
        d["grp" if in_group else "pm"] += 1
        d[f"h.{hour}"] += 1
        if in_group and chat_id:
            _g[(day, chat_id)] = _g.get((day, chat_id), 0) + 1
        _kick()
    except Exception:
        logger.debug("record_activity failed", exc_info=True)


def record_file(uid: int, file_name: str):
    """A file was delivered to this user."""
    try:
        if uid in ADMINS:
            return
        now = _now()
        day, hour = ist_day(at=now), now.astimezone(IST).strftime("%H")
        e = _entry(day, uid)
        e["f"] += 1
        e["fn"].append((file_name or "file")[:70])
        d = _d.setdefault(day, Counter())
        d["files"] += 1
        d[f"fh.{hour}"] += 1
        _kick()
    except Exception:
        logger.debug("record_file failed", exc_info=True)


def record_verify_prompt(tier: int):
    """The 'please verify' message was shown (shortener link handed out)."""
    try:
        _d.setdefault(ist_day(), Counter())[f"vp{int(tier)}"] += 1
        _kick()
    except Exception:
        logger.debug("record_verify_prompt failed", exc_info=True)


async def record_verify_done(uid: int, tier: int):
    """A user completed a shortener. Saved straight away (rare), one per user/tier/day."""
    try:
        from database.settings_db import get_settings
        shorteners = (await get_settings()).get("shorteners", {})
        domain = (shorteners.get(str(tier)) or {}).get("domain", "")
        await _save_verify_event(uid, int(tier), domain, _now())
    except Exception:
        logger.debug("record_verify_done failed", exc_info=True)


async def _save_verify_event(uid: int, tier: int, domain: str, ts: datetime) -> bool:
    await _ensure_indexes()
    day = ist_day(at=ts)
    res = await verify_col.update_one(
        {"_id": f"{day}|{uid}|{tier}"},
        {"$setOnInsert": {"day": day, "uid": uid, "tier": tier, "domain": domain, "ts": ts}},
        upsert=True,
    )
    if res.upserted_id is not None:                     # first time today -> count it
        await daily_col.update_one({"_id": day}, {"$inc": {f"vd{tier}": 1}}, upsert=True)
        return True
    return False


async def _ensure_indexes():
    global _indexes_ready
    if _indexes_ready:
        return
    ttl = KEEP_DAYS * 86400
    await users_col.create_index([("day", ASCENDING)], name="su_day")
    await users_col.create_index([("at", ASCENDING)], expireAfterSeconds=ttl, name="su_ttl")
    await groups_col.create_index([("day", ASCENDING)], name="sg_day")
    await groups_col.create_index([("at", ASCENDING)], expireAfterSeconds=ttl, name="sg_ttl")
    await verify_col.create_index([("day", ASCENDING), ("tier", ASCENDING)], name="sv_day")
    await verify_col.create_index([("ts", ASCENDING)], expireAfterSeconds=ttl, name="sv_ttl")
    _indexes_ready = True


async def flush():
    """Save everything collected so far. Never raises."""
    global _u, _g, _d
    if not (_u or _g or _d):
        return
    u, g, d = _u, _g, _d
    _u, _g, _d = {}, {}, {}
    try:
        await _ensure_indexes()
        now = _now()
        ops = []
        for (day, uid), e in u.items():
            setv = {"last": now}
            if e["nm"]:
                setv["nm"], setv["un"] = e["nm"], e["un"]
            if e["pm"]:
                setv["pm"] = True
            if e["g"]:
                setv["g"] = True
            upd = {"$inc": {"m": e["m"], "f": e["f"]}, "$set": setv,
                   "$setOnInsert": {"day": day, "uid": uid, "at": e["at"]}}
            if e["fn"]:
                upd["$push"] = {"fn": {"$each": e["fn"][-5:], "$slice": -5}}
            ops.append(UpdateOne({"_id": f"{day}|{uid}"}, upd, upsert=True))
        for i in range(0, len(ops), 1000):
            await users_col.bulk_write(ops[i:i + 1000], ordered=False)

        gops = [UpdateOne({"_id": f"{day}|{gid}"}, {"$inc": {"m": n}, "$setOnInsert": {"day": day, "gid": gid, "at": now}},
                          upsert=True) for (day, gid), n in g.items()]
        if gops:
            await groups_col.bulk_write(gops, ordered=False)

        for day, counter in d.items():
            await daily_col.update_one({"_id": day}, {"$inc": dict(counter)}, upsert=True)
    except Exception:
        logger.exception("Could not save the daily statistics (dropped)")


async def _flush_loop():
    while True:
        await asyncio.sleep(FLUSH_EVERY)
        await flush()


# ══════════════════════════════════════════════════════════════════════════════
# READING
# ══════════════════════════════════════════════════════════════════════════════

_COND_REQ = {"$sum": {"$cond": [{"$gt": ["$f", 0]}, 1, 0]}}


async def activity_totals(days: list) -> dict:
    """Unique users over the days: active, requesters, PM / group users, messages, files."""
    await flush()
    pipeline = [
        {"$match": {"day": _range(days)}},
        {"$group": {"_id": "$uid", "f": {"$sum": "$f"}, "m": {"$sum": "$m"},
                    "pm": {"$max": "$pm"}, "g": {"$max": "$g"}}},
        {"$group": {"_id": None, "users": {"$sum": 1}, "req": _COND_REQ,
                    "files": {"$sum": "$f"}, "msgs": {"$sum": "$m"},
                    "pm": {"$sum": {"$cond": [{"$eq": ["$pm", True]}, 1, 0]}},
                    "grp": {"$sum": {"$cond": [{"$eq": ["$g", True]}, 1, 0]}}}},
    ]
    rows = [r async for r in users_col.aggregate(pipeline)]
    r = rows[0] if rows else {}
    return {k: int(r.get(k, 0)) for k in ("users", "req", "files", "msgs", "pm", "grp")}


async def daily_series(days: list) -> dict:
    """day -> {dau, req, files, msgs, h (messages per hour), vd1..3, vp1..3}"""
    await flush()
    out = {d: {"dau": 0, "req": 0, "files": 0, "msgs": 0, "h": {}, "fh": {},
               "vd1": 0, "vd2": 0, "vd3": 0, "vp1": 0, "vp2": 0, "vp3": 0} for d in days}
    pipeline = [
        {"$match": {"day": _range(days)}},
        {"$group": {"_id": "$day", "dau": {"$sum": 1}, "req": _COND_REQ,
                    "files": {"$sum": "$f"}, "msgs": {"$sum": "$m"}}},
    ]
    async for r in users_col.aggregate(pipeline):
        if r["_id"] in out:
            out[r["_id"]].update(dau=int(r["dau"]), req=int(r["req"]), files=int(r["files"]), msgs=int(r["msgs"]))
    async for r in daily_col.find({"_id": _range(days)}):
        if r["_id"] in out:
            row = out[r["_id"]]
            for k in ("vd1", "vd2", "vd3", "vp1", "vp2", "vp3"):
                row[k] = int(r.get(k, 0))
            row["h"], row["fh"] = r.get("h", {}) or {}, r.get("fh", {}) or {}
            # the counters in stat_daily outlive the per-user docs, so use them when the user docs are gone
            row["files"] = row["files"] or int(r.get("files", 0))
            row["msgs"] = row["msgs"] or int(r.get("msgs", 0))
    return out


def hours_total(series: dict, key: str = "h") -> list:
    """Messages (or files, key='fh') per IST hour, summed over the days -> list of 24 ints."""
    total = [0] * 24
    for row in series.values():
        for hh, n in (row.get(key) or {}).items():
            try:
                total[int(hh)] += int(n)
            except (ValueError, IndexError):
                pass
    return total


async def requesters(days: list, skip: int, limit: int) -> tuple:
    """Users who received files in the period, most files first -> (rows, total_users)."""
    await flush()
    match = {"$match": {"day": _range(days), "f": {"$gt": 0}}}
    group = {"$group": {"_id": "$uid", "f": {"$sum": "$f"}, "nm": {"$last": "$nm"}, "un": {"$last": "$un"},
                        "fn": {"$push": "$fn"}}}
    rows = [r async for r in users_col.aggregate([match, group, {"$sort": {"f": -1, "_id": 1}},
                                                  {"$skip": skip}, {"$limit": limit}])]
    total = 0
    async for r in users_col.aggregate([match, {"$group": {"_id": "$uid"}}, {"$count": "n"}]):
        total = r["n"]
    for r in rows:
        names: list = []
        for chunk in r.pop("fn"):
            for n in chunk or []:
                if n not in names:
                    names.append(n)
        r["files"] = names[-4:]
    return rows, int(total)


async def verify_summary(days: list) -> dict:
    """tier -> {done, users, prompts}"""
    await flush()
    out = {t: {"done": 0, "users": 0, "prompts": 0} for t in (1, 2, 3)}
    pipeline = [
        {"$match": {"day": _range(days)}},
        {"$group": {"_id": {"t": "$tier", "u": "$uid"}, "n": {"$sum": 1}}},
        {"$group": {"_id": "$_id.t", "done": {"$sum": "$n"}, "users": {"$sum": 1}}},
    ]
    async for r in verify_col.aggregate(pipeline):
        if r["_id"] in out:
            out[r["_id"]].update(done=int(r["done"]), users=int(r["users"]))
    async for r in daily_col.find({"_id": _range(days)}):
        for t in (1, 2, 3):
            out[t]["prompts"] += int(r.get(f"vp{t}", 0))
    return out


async def verify_events(days: list, tier: int | None, skip: int, limit: int) -> tuple:
    """Completions in the period (newest first) -> (rows, total)."""
    await flush()
    match: dict = {"day": _range(days)}
    if tier:
        match["tier"] = tier
    total = await verify_col.count_documents(match)
    rows = await verify_col.find(match).sort("ts", -1).skip(skip).limit(limit).to_list(limit)
    uids = [r["uid"] for r in rows]
    names = {}
    if uids:
        async for d in db["bot_users"].find({"_id": {"$in": uids}}, {"name": 1, "username": 1}):
            names[d["_id"]] = d
    for r in rows:
        n = names.get(r["uid"]) or {}
        r["nm"], r["un"] = n.get("name"), n.get("username")
    return rows, total


async def backfill_verify_today():
    """The verification table only remembers each user's LATEST completion per tier, so on the
    day this feature is switched on, today's earlier completions are copied from it once.
    Safe to run any number of times (one event per user/tier/day)."""
    try:
        start, _end = day_bounds(ist_day())
        fields = {1: "last_verified", 2: "second_time_verified", 3: "third_time_verified"}
        from database.settings_db import get_settings
        shorteners = (await get_settings()).get("shorteners", {})
        for tier, field in fields.items():
            domain = (shorteners.get(str(tier)) or {}).get("domain", "")
            async for d in db["verify_status"].find({field: {"$gte": start.replace(tzinfo=None)}}, {field: 1}):
                ts = d[field]
                ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
                await _save_verify_event(d["_id"], tier, domain, ts)
    except Exception:
        logger.debug("backfill_verify_today failed", exc_info=True)


async def group_activity(days: list, limit: int) -> tuple:
    """Busiest groups -> (rows [{_id: gid, m}], number of active groups)."""
    await flush()
    match = {"$match": {"day": _range(days)}}
    rows = [r async for r in groups_col.aggregate([match, {"$group": {"_id": "$gid", "m": {"$sum": "$m"}}},
                                                   {"$sort": {"m": -1}}, {"$limit": limit}])]
    n = 0
    async for r in groups_col.aggregate([match, {"$group": {"_id": "$gid"}}, {"$count": "n"}]):
        n = r["n"]
    return rows, int(n)


async def tracking_since() -> str | None:
    """The first day daily numbers exist for (None until something has been recorded)."""
    await flush()
    doc = await daily_col.find_one({}, sort=[("_id", 1)])
    return doc["_id"] if doc else None


async def retention(cohort_from: int = 14, cohort_to: int = 8, active_days: int = 7) -> dict:
    """Of the users who started the bot `cohort_to`..`cohort_from` days ago, how many were
    active during the last `active_days` days."""
    await flush()
    a = _now() - timedelta(days=cohort_from)
    b = _now() - timedelta(days=cohort_to - 1)
    ids = [d["_id"] async for d in db["users"].find({"joined_at": {"$gte": a, "$lt": b}}, {"_id": 1}).limit(20000)]
    if not ids:
        return {"cohort": 0, "back": 0}
    back = 0
    recent = days_back(active_days)
    for i in range(0, len(ids), 5000):
        chunk = ids[i:i + 5000]
        seen = await users_col.distinct("uid", {"day": _range(recent), "uid": {"$in": chunk}})
        back += len(seen)
    return {"cohort": len(ids), "back": back}


# ── things that come from the bot's existing collections ─────────────────────

async def indexed_per_day(days: list, channel_id: int | None = None) -> dict:
    """Files added per IST day. A file's _id is created the moment it is indexed, so the
    _id itself is the 'indexed at' time (always indexed, no extra field needed)."""
    out = {}
    for day in days:
        a, b = day_bounds(day)
        q: dict = {"_id": {"$gte": ObjectId.from_datetime(a), "$lt": ObjectId.from_datetime(b)}}
        if channel_id is not None:
            q["channel_id"] = channel_id
        out[day] = await files.count_documents(q)
    return out


async def new_users_per_day(days: list) -> dict:
    """Users who first started the bot, per IST day (from the `users` collection)."""
    a, _ = day_bounds(days[0])
    _, b = day_bounds(days[-1])
    out = {d: 0 for d in days}
    async for d in db["users"].find({"joined_at": {"$gte": a, "$lt": b}}, {"joined_at": 1}):
        ts = d.get("joined_at")
        if ts is None:
            continue
        ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        day = ist_day(at=ts)
        if day in out:
            out[day] += 1
    return out


async def search_summary(days: list) -> dict:
    """Search-log numbers for the period: total, hits, misses, by outcome, searchers, PM/group."""
    await flush_queries()
    a, _ = day_bounds(days[0])
    _, b = day_bounds(days[-1])
    match = {"$match": {"ts": {"$gte": a, "$lt": b}}}
    by_outcome, pm, grp = {}, 0, 0
    async for r in queries.aggregate([match, {"$group": {"_id": {"o": "$o", "c": "$c"}, "n": {"$sum": 1}}}]):
        o, c, n = r["_id"].get("o"), r["_id"].get("c"), int(r["n"])
        by_outcome[o] = by_outcome.get(o, 0) + n
        if c == "p":
            pm += n
        else:
            grp += n
    searchers = 0
    async for r in queries.aggregate([match, {"$group": {"_id": "$u"}}, {"$count": "n"}]):
        searchers = r["n"]
    total = sum(by_outcome.values())
    hits = sum(by_outcome.get(o, 0) for o in HIT_OUTCOMES)
    return {"total": total, "hits": hits, "misses": total - hits, "by_outcome": by_outcome,
            "searchers": int(searchers), "pm": pm, "grp": grp}


async def top_queries(days: list, limit: int = 10, misses_only: bool = False) -> list:
    await flush_queries()
    a, _ = day_bounds(days[0])
    _, b = day_bounds(days[-1])
    m: dict = {"ts": {"$gte": a, "$lt": b}}
    if misses_only:
        m["o"] = {"$in": list(MISS_OUTCOMES)}
    pipeline = [{"$match": m},
                {"$group": {"_id": "$qn", "q": {"$last": "$q"}, "n": {"$sum": 1}, "o": {"$last": "$o"}, "t": {"$last": "$t"}}},
                {"$sort": {"n": -1}}, {"$limit": limit}]
    return [r async for r in queries.aggregate(pipeline)]


async def searches_per_day(days: list) -> dict:
    await flush_queries()
    out = {}
    for day in days:
        a, b = day_bounds(day)
        out[day] = await queries.count_documents({"ts": {"$gte": a, "$lt": b}})
    return out


async def flush_queries():
    """Make sure buffered search-log entries are saved before counting them."""
    try:
        from database.query_log_db import _flush
        await _flush()
    except Exception:
        pass
