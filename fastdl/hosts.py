"""
Decides which server a new download/watch link points at — automatic failover.

  Tier 1  Oracle   (only if ORACLE_STREAM_URL is set)
          live /health check (short timeout) + month usage + file < 85% of its limit
  Tier 2  Local    (this Koyeb/Render app, only if STREAM_BASE_URL is set)
          stream pool running + month usage + file < 85% of its limit
  Nothing left -> choose_host() returns None and the bot shows the "busy" message.

A tier that isn't configured is simply off. Tokens are HMAC-signed with the same
STREAM_SECRET on every host and contain no host name, so a link made for one host
also works on the other (this is what lets Oracle redirect to Koyeb).

Bandwidth is the REAL bytes each host has sent this month (see fastdl/usage.py):
  local  -> counted in this process
  oracle -> reported by Oracle's /health reply ({"bytes": ...})
An older Oracle server that only answers "ok" has no counter, so for it we fall back
to the old behaviour of reserving the file size when a link is issued.
"""
import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

from config import (
    ORACLE_STREAM_URL, ORACLE_BANDWIDTH_LIMIT_GB,
    STREAM_BASE_URL, STREAM_BANDWIDTH_LIMIT_GB, STREAM_SWITCH_PERCENT,
    STREAM_HEALTH_TIMEOUT, STREAM_LOCAL_BASELINE_GB,
)
from database.stream_db import add_usage, get_usage
from fastdl.pool import pool
from fastdl.usage import counter

logger = logging.getLogger(__name__)
GB = 1024 ** 3


@dataclass
class Host:
    key: str
    base_url: str
    limit_bytes: float


def configured_hosts() -> list[Host]:
    """Tier order. A host whose URL variable is empty is left out (feature off)."""
    hosts = []
    if ORACLE_STREAM_URL:
        hosts.append(Host("oracle", ORACLE_STREAM_URL, ORACLE_BANDWIDTH_LIMIT_GB * GB))
    if STREAM_BASE_URL:
        hosts.append(Host("local", STREAM_BASE_URL, STREAM_BANDWIDTH_LIMIT_GB * GB))
    return hosts


# ---- Oracle health probe ---------------------------------------------------
_UP_TTL, _DOWN_TTL = 30, 10          # re-check a healthy host every 30 s, a dead one every 10 s
_health_cache: dict[str, tuple[bool, float, dict]] = {}
_reports_usage: dict[str, bool] = {}  # does this host's /health include its own byte counter?


async def _probe(host: Host) -> tuple[bool, dict]:
    cached = _health_cache.get(host.key)
    if cached and time.time() - cached[1] < (_UP_TTL if cached[0] else _DOWN_TTL):
        return cached[0], cached[2]
    ok, info = False, {}
    async def _get():
        t = aiohttp.ClientTimeout(total=STREAM_HEALTH_TIMEOUT, connect=STREAM_HEALTH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(f"{host.base_url}/health") as r:
                if r.status != 200:
                    return False, {}
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = {}  # old server: plain "ok"
                data = data if isinstance(data, dict) else {}
                return data.get("workers") != 0, data  # up but with no stream client = unusable
    try:
        # outer guard so a stuck DNS / TLS step can never hold up the user's button tap
        ok, info = await asyncio.wait_for(_get(), STREAM_HEALTH_TIMEOUT + 0.5)
    except Exception:
        ok, info = False, {}
    _reports_usage[host.key] = "bytes" in info
    if not ok:
        logger.warning("Stream host %s failed its health check (%.1fs timeout)", host.key, STREAM_HEALTH_TIMEOUT)
    _health_cache[host.key] = (ok, time.time(), info)
    return ok, info


# ---- per-tier checks -------------------------------------------------------
async def _check(host: Host, size: int) -> tuple[bool, str]:
    info = {}
    if host.key == "local":
        # this process must have stream clients; if the pool is empty try to bring it back
        if not pool.workers and not await pool.ensure_workers():
            return False, "no stream client is running (see 'Fast-download pool' lines at boot)"
        used = await counter.month_bytes() + int(STREAM_LOCAL_BASELINE_GB * GB)
    else:
        ok, info = await _probe(host)
        if not ok:
            return False, "health check failed / offline"
        used = int(info["bytes"]) if "bytes" in info else await get_usage(host.key)
    cutoff = host.limit_bytes * STREAM_SWITCH_PERCENT / 100
    if used + size > cutoff:
        return False, (f"bandwidth: month usage {used / GB:.1f} GB + file {size / GB:.2f} GB "
                       f"is past {STREAM_SWITCH_PERCENT}% of its {host.limit_bytes / GB:.0f} GB limit")
    return True, ""


async def choose_host(size: int) -> Host | None:
    hosts = configured_hosts()
    if not hosts:
        logger.error("No stream host configured — set STREAM_BASE_URL and/or ORACLE_STREAM_URL")
        return None
    for i, host in enumerate(hosts, 1):
        ok, why = await _check(host, size)
        if ok:
            if i > 1:
                logger.info("Fast download: using tier %s (%s)", i, host.key)
            return host
        logger.warning("Stream tier %s (%s) skipped: %s", i, host.key, why)
    return None


async def note_link_issued(host: Host, size: int):
    """Called after a link is created. Real usage is counted while bytes are sent, so nothing is
    reserved — except for an old Oracle server that has no counter of its own."""
    if host.key != "local" and not _reports_usage.get(host.key):
        await add_usage(host.key, size)
