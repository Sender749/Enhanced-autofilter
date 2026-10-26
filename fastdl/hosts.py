"""
Decides which server a new download link points at.

Order: Oracle (if ORACLE_STREAM_URL is set) -> this deployment (Koyeb/Render).
A host is skipped when it's down or when this month's reserved bandwidth plus
the new file would pass STREAM_SWITCH_PERCENT of its limit. Usage is reserved
when a link is issued (full file size) — slightly pessimistic, but needs no
reporting back from the stream servers, so Oracle never touches the database.
"""
import logging
import time
from dataclasses import dataclass

import aiohttp

from config import (
    ORACLE_STREAM_URL, ORACLE_BANDWIDTH_LIMIT_GB,
    STREAM_BASE_URL, STREAM_BANDWIDTH_LIMIT_GB, STREAM_SWITCH_PERCENT,
)
from database.stream_db import get_usage
from fastdl.pool import pool

logger = logging.getLogger(__name__)
GB = 1024 ** 3


@dataclass
class Host:
    key: str
    base_url: str
    limit_bytes: float


def configured_hosts() -> list[Host]:
    hosts = []
    if ORACLE_STREAM_URL:  # empty = Oracle stays disabled
        hosts.append(Host("oracle", ORACLE_STREAM_URL, ORACLE_BANDWIDTH_LIMIT_GB * GB))
    if STREAM_BASE_URL:
        hosts.append(Host("local", STREAM_BASE_URL, STREAM_BANDWIDTH_LIMIT_GB * GB))
    return hosts


_health_cache: dict[str, tuple[bool, float]] = {}


async def _is_up(host: Host) -> bool:
    cached = _health_cache.get(host.key)
    if cached and time.time() - cached[1] < 30:
        return cached[0]
    ok = False
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{host.base_url}/health") as r:
                ok = r.status == 200
    except Exception:
        ok = False
    if not ok:
        logger.warning("Stream host %s failed its health check", host.key)
    _health_cache[host.key] = (ok, time.time())
    return ok


async def choose_host(size: int) -> Host | None:
    hosts = configured_hosts()
    if not hosts:
        logger.error("No stream host configured — set STREAM_BASE_URL (or ORACLE_STREAM_URL)")
        return None
    for host in hosts:
        if host.key == "local":
            # This process must have stream clients running; if the pool is empty try to revive it.
            if not pool.workers and not await pool.ensure_workers():
                logger.error("Stream host 'local' skipped: no stream client is running "
                             "(check the 'Stream client' / 'Fast-download pool' lines at boot)")
                continue
        elif not await _is_up(host):
            continue
        used = await get_usage(host.key)
        if used + size > host.limit_bytes * STREAM_SWITCH_PERCENT / 100:
            logger.warning("Stream host %s skipped: month usage %.1f GB + file %.2f GB is past %s%% of its %.0f GB limit",
                           host.key, used / GB, size / GB, STREAM_SWITCH_PERCENT, host.limit_bytes / GB)
            continue
        return host
    return None
