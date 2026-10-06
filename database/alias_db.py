"""
Search aliases + the "file not found" reports that admins turn into aliases.

Two small collections:

  nf_misses  one document per #FILE_NOT_FOUND report posted in the admin channel
             {_id, query, key, user_id, text, chat_msg_id, followup_msg_id,
              state: open|waiting|confirm|done|ignored,
              pending_title, pending_key, created}
             Kept in Mongo (not memory) so the buttons keep working after a
             restart. Auto-expires after MISS_TTL_DAYS.

  aliases    {_id: <normalised query title>, target_key, target_title, created}
             "hindi madium" -> "hindi medium". Applied by search_files() to the
             TITLE part of a query only, so year / language / quality tags the
             user typed still filter the result as usual.

Aliases are cached in memory (loaded once, updated on every change), so the
lookup done at the start of every search costs a dict lookup, not a DB trip.
"""
import logging
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from database.client import db
from database.filters_db import clean_title, parse_query

logger = logging.getLogger(__name__)

misses = db["nf_misses"]
aliases = db["search_aliases"]

MISS_TTL_DAYS = 90

_indexes_ready = False
_alias_cache: dict | None = None  # key -> target_key


def alias_key(text: str) -> str:
    """Normalised title of a query — exactly the key search_files() computes."""
    return clean_title(parse_query(text.strip())[0]).lower()


async def _ensure_indexes():
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        await misses.create_index([("created", ASCENDING)], expireAfterSeconds=MISS_TTL_DAYS * 86400, name="miss_ttl")
        await misses.create_index([("chat_msg_id", ASCENDING)], name="miss_msg")
        await misses.create_index([("key", ASCENDING), ("state", ASCENDING)], name="miss_key_state")
        _indexes_ready = True
    except Exception:
        logger.exception("Could not create alias indexes")


# ── alias cache ─────────────────────────────────────────────────────────────

async def _load_cache() -> dict:
    global _alias_cache
    if _alias_cache is None:
        cache = {}
        async for doc in aliases.find({}, {"target_key": 1}):
            cache[doc["_id"]] = doc["target_key"]
        _alias_cache = cache
    return _alias_cache


async def get_alias(key: str) -> str | None:
    """Target title key for a query key, or None. One hop only (no chains)."""
    if not key:
        return None
    try:
        cache = await _load_cache()
    except Exception:
        logger.exception("Alias cache load failed")
        return None
    target = cache.get(key)
    return target if target and target != key else None


async def save_alias(key: str, target_key: str, target_title: str, by: str = "admin") -> None:
    await aliases.update_one(
        {"_id": key},
        {"$set": {"target_key": target_key, "target_title": target_title, "by": by,
                  "created": datetime.now(timezone.utc)}},
        upsert=True,
    )
    cache = await _load_cache()
    cache[key] = target_key


async def delete_alias(key: str) -> bool:
    result = await aliases.delete_one({"_id": key})
    cache = await _load_cache()
    cache.pop(key, None)
    return result.deleted_count > 0


async def list_aliases(limit: int = 40) -> tuple[list, int]:
    total = await aliases.count_documents({})
    docs = await aliases.find({}).sort("created", DESCENDING).limit(limit).to_list(length=limit)
    return docs, total


# ── misses ──────────────────────────────────────────────────────────────────

def _oid(miss_id: str):
    try:
        return ObjectId(miss_id)
    except (InvalidId, TypeError):
        return None


async def create_miss(query: str, key: str, user_id: int, text: str) -> str:
    await _ensure_indexes()
    res = await misses.insert_one({
        "query": query, "key": key, "user_id": user_id, "text": text,
        "chat_msg_id": None, "followup_msg_id": None, "state": "open",
        "pending_title": None, "pending_key": None,
        "created": datetime.now(timezone.utc),
    })
    return str(res.inserted_id)


async def set_miss_message(miss_id: str, chat_msg_id: int) -> None:
    await misses.update_one({"_id": _oid(miss_id)}, {"$set": {"chat_msg_id": chat_msg_id}})


async def get_miss(miss_id: str) -> dict | None:
    oid = _oid(miss_id)
    return await misses.find_one({"_id": oid}) if oid else None


async def transition(miss_id: str, from_states: list, new_state: str, extra: dict | None = None) -> dict | None:
    """Atomically move a miss between states. Returns the updated doc, or None
    if it was not in one of `from_states` (someone else already handled it) —
    this is what makes 'first admin to click wins' safe."""
    oid = _oid(miss_id)
    if not oid:
        return None
    update = {"state": new_state}
    if extra:
        update.update(extra)
    return await misses.find_one_and_update(
        {"_id": oid, "state": {"$in": from_states}},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )


async def find_waiting_by_reply(reply_to_id: int) -> dict | None:
    """The miss an admin's channel reply is answering (reply goes to the report
    message itself, or to the 'no match' follow-up the bot posted under it)."""
    return await misses.find_one({
        "state": {"$in": ["waiting", "confirm"]},
        "$or": [{"chat_msg_id": reply_to_id}, {"followup_msg_id": reply_to_id}],
    })


async def other_open_with_key(key: str, exclude_id, limit: int = 20) -> list:
    return await misses.find(
        {"key": key, "state": "open", "_id": {"$ne": exclude_id}}
    ).limit(limit).to_list(length=limit)
