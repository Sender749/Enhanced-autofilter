"""
Three-tier verification, ported from the reference bot's behaviour:

  Tier 1 — must be completed once per calendar day (IST). Grants access for
           the rest of that day.
  Tier 2 — if `verify_time` seconds have passed since the tier-1 completion
           (even though still "today"), tier 2 must be completed again
           before another file is delivered.
  Tier 3 — same idea, `third_verify_time` seconds after the tier-2
           completion.

Each tier uses its own shortener (settings_db.shorteners), so a user who
keeps requesting files throughout the day cycles through progressively
later tiers rather than being asked to verify from scratch every time.
"""
import asyncio
import logging
import random
import string
from datetime import datetime, timedelta, timezone

from database.client import db
from database.stats_db import record_verify_done
from utils import IST, ensure_utc

logger = logging.getLogger(__name__)

verify_status_col = db["verify_status"]
verify_tokens_col = db["verify_tokens"]
ad_rewards_col = db["ad_rewards"]   # AdsGram "ad watched" confirmations, waiting to be used

_TOKEN_TTL = timedelta(minutes=30)


def _now():
    return datetime.now(timezone.utc)


def _ist_midnight_utc(now_utc: datetime) -> datetime:
    """UTC instant corresponding to the most recent local midnight in IST."""
    ist_now = now_utc.astimezone(IST)
    ist_midnight = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return ist_midnight.astimezone(timezone.utc)


async def ensure_indexes():
    await verify_tokens_col.create_index("created_at", expireAfterSeconds=int(_TOKEN_TTL.total_seconds()))
    await ad_rewards_col.create_index("created_at", expireAfterSeconds=900)


async def _get_status(user_id: int) -> dict:
    doc = await verify_status_col.find_one({"_id": user_id}) or {}
    return {
        "last_verified": ensure_utc(doc.get("last_verified")),
        "second_time_verified": ensure_utc(doc.get("second_time_verified")),
        "third_time_verified": ensure_utc(doc.get("third_time_verified")),
    }


async def required_tier(user_id: int, verify_time: int, third_verify_time: int) -> int | None:
    """Return the tier (1, 2, or 3) the user must complete, or None if clear."""
    status = await _get_status(user_id)
    last = status.get("last_verified")
    second = status.get("second_time_verified")
    third = status.get("third_time_verified")
    now = _now()

    if last is None:
        return 1

    verified_today = (now - last) <= (now - _ist_midnight_utc(now))
    if not verified_today:
        return 1

    since_last = (now - last).total_seconds()
    if since_last <= verify_time:
        return None  # tier 1 still fresh — fully clear

    # Tier 1's window has elapsed. Tier 2 clears this *unless* its own
    # window (relative to when tier 2 was last done) has also elapsed.
    if second is None or second < last:
        return 2
    since_second = (now - second).total_seconds()
    if since_second <= third_verify_time:
        return None  # tier 2 still fresh
    if third is None or third < second:
        return 3
    return None


async def mark_verified(user_id: int, tier: int):
    field = {1: "last_verified", 2: "second_time_verified", 3: "third_time_verified"}[tier]
    await verify_status_col.update_one(
        {"_id": user_id},
        {"$set": {field: _now()}},
        upsert=True,
    )
    await record_verify_done(user_id, tier)     # daily stats (/stats panel); never raises


# ── short-lived tokens used in the notcopy_<token> deep link ────────────────

def _new_token() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=10))


async def create_token(user_id: int, tier: int, file_id: str) -> str:
    token = _new_token()
    await verify_tokens_col.insert_one({
        "_id": token,
        "user_id": user_id,
        "tier": tier,
        "file_id": file_id,
        "used": False,
        "created_at": _now(),
    })
    return token


async def consume_token(token: str, user_id: int) -> dict | None:
    """Atomically mark a token used and return it, or None if invalid/expired/
    already used/belongs to someone else."""
    doc = await verify_tokens_col.find_one_and_update(
        {"_id": token, "user_id": user_id, "used": False},
        {"$set": {"used": True}},
    )
    return doc


async def peek_token(token: str, user_id: int) -> dict | None:
    """Look at a token WITHOUT using it (the ad page checks it before and after the ad)."""
    return await verify_tokens_col.find_one({"_id": token, "user_id": user_id, "used": False})


# ── AdsGram: "this user watched an ad" confirmations ─────────────────────────
# AdsGram's server calls our /adsgram/reward link once per watched ad. Each call adds 1;
# each verification uses 1, so one ad can never unlock two files.

async def record_ad_reward(user_id: int):
    await ad_rewards_col.update_one(
        {"_id": user_id},
        {"$inc": {"n": 1}, "$set": {"created_at": _now()}},
        upsert=True,
    )


async def consume_ad_reward(user_id: int) -> bool:
    doc = await ad_rewards_col.find_one_and_update(
        {"_id": user_id, "n": {"$gt": 0}},
        {"$inc": {"n": -1}},
    )
    return bool(doc)


async def wait_ad_reward(user_id: int, timeout: float) -> bool:
    """The page's "ad finished" and AdsGram's server call can arrive in either order —
    wait a few seconds for the confirmation instead of failing at once."""
    waited = 0.0
    while True:
        if await consume_ad_reward(user_id):
            return True
        if waited >= timeout:
            return False
        await asyncio.sleep(0.5)
        waited += 0.5
