"""
Storage for the AI-learning feature (plugins/learn.py).

  learn_events   what users did around a search — kept 60 days, then deleted
                   kind "miss"     a search that found nothing (even after the
                                    fuzzy / related stages)
                   kind "pair"     "searched A (failed), then got title B":
                                    source "retry" (searched again and it worked)
                                    or "tap" (tapped a suggestion / related title)
                   kind "request"  the user pressed Request / sent /req
                 Users are stored only as a short salted hash, never as an ID.
  learn_cands    suggestions the AI made for admins (alias / filterword / noise)
                   state: pending -> posted -> approved | rejected | undone | changing
  learn_blocked  suggestions an admin rejected or undid — never suggested again
  learn_noise    admin-approved "ignore this word at the start of a title" list
  learn_meta     small key/value bookkeeping (e.g. when the noise scan last ran)
"""
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from database.client import db

logger = logging.getLogger(__name__)

events = db["learn_events"]
cands = db["learn_cands"]
blocked = db["learn_blocked"]
noise_col = db["learn_noise"]
meta = db["learn_meta"]

EVENT_TTL_DAYS = 60
_SALT = os.environ.get("BOT_TOKEN", "")[:20] or "autofilter"
_indexes_ready = False


def uhash(user_id: int) -> str:
    """Short salted hash — enough to count DISTINCT users, useless for finding one."""
    return hashlib.sha1(f"{_SALT}:{user_id}".encode()).hexdigest()[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_indexes():
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        await events.create_index([("ts", ASCENDING)], expireAfterSeconds=EVENT_TTL_DAYS * 86400, name="ev_ttl")
        await events.create_index([("kind", ASCENDING), ("qkey", ASCENDING)], name="ev_kind_q")
        await cands.create_index([("type", ASCENDING), ("key", ASCENDING), ("target", ASCENDING)], name="cand_ident")
        await cands.create_index([("state", ASCENDING)], name="cand_state")
        await blocked.create_index([("type", ASCENDING), ("key", ASCENDING), ("target", ASCENDING)], name="blk_ident")
        _indexes_ready = True
    except Exception:
        logger.exception("Could not create learning indexes")


# ── events ──────────────────────────────────────────────────────────────────

async def add_event(kind: str, user_id: int, qkey: str, query: str,
                    tkey: str | None = None, target: str | None = None, source: str | None = None):
    await ensure_indexes()
    await events.insert_one({
        "kind": kind, "u": uhash(user_id), "qkey": qkey, "query": (query or "")[:120],
        "tkey": tkey, "target": target, "source": source, "ts": _now(),
    })


async def pair_groups(days: int = 30) -> list:
    """Failed-query -> title pairs, grouped, with how many DISTINCT users made each."""
    since = _now() - timedelta(days=days)
    groups: dict = {}
    async for ev in events.find({"kind": "pair", "ts": {"$gte": since}}):
        g = groups.setdefault((ev["qkey"], ev["tkey"]), {
            "qkey": ev["qkey"], "tkey": ev["tkey"], "query": ev.get("query", ""),
            "target": ev.get("target") or ev["tkey"], "users": set(), "sources": set(),
        })
        g["users"].add(ev["u"])
        if ev.get("source"):
            g["sources"].add(ev["source"])
    out = []
    for g in groups.values():
        g["users"] = len(g["users"])
        out.append(g)
    return out


async def miss_events(days: int = 30) -> list:
    since = _now() - timedelta(days=days)
    return [ev async for ev in events.find({"kind": "miss", "ts": {"$gte": since}}, {"u": 1, "query": 1, "qkey": 1})]


async def top_missing(limit: int = 10, days: int = 30) -> list:
    """Most-wanted titles the bot couldn't find: [(query, distinct users, requests)]."""
    since = _now() - timedelta(days=days)
    groups: dict = {}
    async for ev in events.find({"kind": {"$in": ["miss", "request"]}, "ts": {"$gte": since}}):
        g = groups.setdefault(ev["qkey"], {"query": ev.get("query", ev["qkey"]), "users": set(), "requests": 0})
        g["users"].add(ev["u"])
        if ev["kind"] == "request":
            g["requests"] += 1
    ranked = sorted(groups.values(), key=lambda g: (-len(g["users"]), -g["requests"]))
    return [(g["query"], len(g["users"]), g["requests"]) for g in ranked[:limit]]


async def event_count() -> int:
    return await events.estimated_document_count()


# ── candidates ──────────────────────────────────────────────────────────────

def _oid(cand_id: str):
    try:
        return ObjectId(cand_id)
    except (InvalidId, TypeError):
        return None


async def cand_find(ctype: str, key: str, target: str = ""):
    return await cands.find_one({"type": ctype, "key": key, "target": target})


async def cand_create(ctype: str, key: str, target: str, fields: dict) -> dict:
    await ensure_indexes()
    doc = {"type": ctype, "key": key, "target": target, "state": "pending",
           "msg_id": None, "created": _now(), **fields}
    res = await cands.insert_one(doc)
    doc["_id"] = res.inserted_id
    return doc


async def cand_get(cand_id: str):
    oid = _oid(cand_id)
    return await cands.find_one({"_id": oid}) if oid else None


async def cand_transition(cand_id: str, from_states: list, new_state: str, extra: dict | None = None):
    """Atomic state change — first admin to press a button wins."""
    oid = _oid(cand_id)
    if not oid:
        return None
    update = {"state": new_state, "updated": _now()}
    if extra:
        update.update(extra)
    return await cands.find_one_and_update(
        {"_id": oid, "state": {"$in": from_states}}, {"$set": update},
        return_document=ReturnDocument.AFTER,
    )


async def cand_set(cand_id, fields: dict):
    await cands.update_one({"_id": cand_id}, {"$set": fields})


async def cands_in_state(states: list, limit: int = 8, ctype: str | None = None) -> list:
    q = {"state": {"$in": states}}
    if ctype:
        q["type"] = ctype
    return await cands.find(q).sort("updated", DESCENDING).limit(limit).to_list(length=limit)


async def posted_since(hours: int = 24) -> int:
    return await cands.count_documents({"posted_at": {"$gte": _now() - timedelta(hours=hours)}})


async def state_counts() -> dict:
    out = {}
    for state in ("posted", "approved", "rejected", "undone"):
        out[state] = await cands.count_documents({"state": state})
    return out


# ── blocklist ───────────────────────────────────────────────────────────────

async def block(ctype: str, key: str, target: str = ""):
    await ensure_indexes()
    await blocked.update_one({"type": ctype, "key": key, "target": target},
                             {"$set": {"ts": _now()}}, upsert=True)


async def is_blocked(ctype: str, key: str, target: str = "") -> bool:
    return await blocked.find_one({"type": ctype, "key": key, "target": target}) is not None


async def blocked_count() -> int:
    return await blocked.estimated_document_count()


async def clear_blocked() -> int:
    res = await blocked.delete_many({})
    return res.deleted_count


# ── noise words ─────────────────────────────────────────────────────────────

async def load_noise() -> set:
    return {doc["_id"] async for doc in noise_col.find({})}


async def add_noise(word: str):
    await noise_col.update_one({"_id": word.lower()}, {"$set": {"ts": _now()}}, upsert=True)


async def remove_noise(word: str):
    await noise_col.delete_one({"_id": word.lower()})


# ── bookkeeping / reset ─────────────────────────────────────────────────────

async def meta_get(key: str, default=None):
    doc = await meta.find_one({"_id": key})
    return doc["value"] if doc else default


async def meta_set(key: str, value):
    await meta.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)


async def reset_learning() -> tuple:
    """Forget what was observed and every NOT-yet-decided suggestion. Approved
    aliases / filter words / noise words are untouched (undo those one by one)."""
    ev = await events.delete_many({})
    pending = await cands.delete_many({"state": {"$in": ["pending", "posted"]}})
    return ev.deleted_count, pending.deleted_count
