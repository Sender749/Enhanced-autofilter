"""
Every search a user sends, saved so admins can see what people really type
and what the bot did with it (found it / fixed a typo / only offered
suggestions / found nothing), then export it as a text file — the same idea as
"Export All Captions", but for queries.

One document per search in `search_queries`:
    q   the text exactly as typed (max 200 chars)      u   salted hash of the user
    qn  lower-cased, spaces collapsed (for grouping)   o   outcome (see OUTCOMES)
    n   how many files were shown                      t   title the bot resolved it to
    c   "p" private chat / "g" group                   ts  when

Writes are buffered and inserted in bulk every few seconds, so logging never
slows a user's search; if the database write fails the batch is dropped and
logged — a search is never affected. Entries delete themselves after
QUERY_LOG_DAYS (default 90).
"""
import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from pymongo import ASCENDING

from database.client import db

logger = logging.getLogger(__name__)

queries = db["search_queries"]

QUERY_LOG_DAYS = int(os.environ.get("QUERY_LOG_DAYS", "90"))
FLUSH_EVERY = 5          # seconds
FLUSH_AT = 100           # or as soon as this many are waiting
MAX_EXPORT_ROWS = 300_000
_SALT = os.environ.get("BOT_TOKEN", "")[:20] or "autofilter"

OUTCOMES = {
    "found": "found it",
    "fuzzy": "typo fixed",
    "relaxed": "title found, extra tag (season/year/…) missing",
    "related": "only related titles offered",
    "suggested": "only outside suggestions offered",
    "none": "nothing found",
}
MISSED = {"related", "suggested", "none"}

_buffer: list = []
_flusher: asyncio.Task | None = None
_indexes_ready = False


def _uhash(user_id) -> str:
    return hashlib.sha1(f"{_SALT}:{user_id}".encode()).hexdigest()[:12] if user_id else ""


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


async def _flush():
    global _buffer, _indexes_ready
    if not _buffer:
        return
    batch, _buffer = _buffer, []
    try:
        if not _indexes_ready:
            await queries.create_index([("ts", ASCENDING)], expireAfterSeconds=QUERY_LOG_DAYS * 86400, name="q_ttl")
            _indexes_ready = True
        await queries.insert_many(batch, ordered=False)
    except Exception:
        logger.exception("Could not save %s search queries (dropped)", len(batch))


async def _flush_loop():
    while True:
        await asyncio.sleep(FLUSH_EVERY)
        await _flush()


async def log_query(user_id, query: str, outcome: str, shown: int = 0, title: str = "", private: bool = False):
    """Queue one search for saving. Never raises."""
    global _flusher
    try:
        q = (query or "").strip()[:200]
        if not q:
            return
        _buffer.append({
            "q": q, "qn": normalize(q), "u": _uhash(user_id), "o": outcome, "n": shown,
            "t": (title or "")[:120], "c": "p" if private else "g", "ts": datetime.now(timezone.utc),
        })
        if _flusher is None or _flusher.done():
            _flusher = asyncio.create_task(_flush_loop())
        if len(_buffer) >= FLUSH_AT:
            await _flush()
    except Exception:
        logger.debug("log_query failed", exc_info=True)


async def export_queries(path: str, days: int | None = None, missed_only: bool = False) -> tuple:
    """Write unique queries (most-searched first) to a text file.
    Returns (unique_queries, total_searches)."""
    await _flush()
    match: dict = {}
    if days:
        match["ts"] = {"$gte": datetime.now(timezone.utc) - timedelta(days=days)}

    groups: dict = {}
    cursor = queries.find(match).sort("ts", -1).limit(MAX_EXPORT_ROWS)  # newest first
    total = 0
    async for doc in cursor:
        total += 1
        g = groups.get(doc["qn"])
        if g is None:  # first seen = the newest search of this text
            g = groups[doc["qn"]] = {"q": doc["q"], "count": 0, "users": set(), "o": doc["o"],
                                     "n": doc.get("n", 0), "t": doc.get("t", ""), "ts": doc["ts"]}
        g["count"] += 1
        if doc.get("u"):
            g["users"].add(doc["u"])

    rows = [g for g in groups.values() if not missed_only or g["o"] in MISSED]
    rows.sort(key=lambda g: (-g["count"], -g["ts"].timestamp()))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    scope = f"last {days} days" if days else f"everything stored (up to {QUERY_LOG_DAYS} days)"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"# User search queries — exported {now}\n")
        fh.write(f"# {scope} · {len(rows)} unique queries · {total} searches"
                 + (" · only searches that were NOT found" if missed_only else "") + "\n")
        fh.write("# columns: searches | users | outcome | files shown | query | → title the bot resolved it to\n")
        fh.write("# outcome: " + "; ".join(f"{k} = {v}" for k, v in OUTCOMES.items()) + "\n")
        for g in rows:
            line = f"{g['count']} | {len(g['users'])} | {g['o']} | {g['n']} | {g['q']}"
            if g["t"]:
                line += f" | → {g['t']}"
            fh.write(line.replace("\n", " ") + "\n")
    return len(rows), total


async def stats() -> dict:
    await _flush()
    return {"searches": await queries.estimated_document_count()}
