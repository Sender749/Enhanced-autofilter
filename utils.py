import re
import time
from datetime import timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def ensure_utc(dt):
    """Normalize a datetime read back from Mongo to timezone-aware UTC.
    Belt-and-suspenders alongside the client's tz_aware=True — some
    drivers/proxies don't honor that flag, and a naive/aware comparison
    crashes instead of just being wrong, so this is worth the tiny cost."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Temp:
    """Small process-wide state — bot identity, index-job cancel flag."""
    BOT = None
    U_NAME = ""   # bot username, no @
    CANCEL_INDEX = False


temp = Temp()


def human_size(size: int) -> str:
    if not size:
        return "N/A"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def readable_time(seconds: float) -> str:
    seconds = int(seconds)
    periods = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    parts = []
    for name, secs in periods:
        val, seconds = divmod(seconds, secs)
        if val:
            parts.append(f"{val}{name}")
    return " ".join(parts) or "0s"


def format_duration(seconds) -> str:
    """Seconds -> plain words:  60 -> "1 minute", 3600 -> "1 hour",
    86400 -> "1 day", 5400 -> "1 hour 30 minutes", 45 -> "45 seconds"."""
    seconds = max(0, int(seconds))
    if seconds == 0:
        return "0 seconds"
    parts = []
    for name, secs in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        val, seconds = divmod(seconds, secs)
        if val:
            parts.append(f"{val} {name}{'' if val == 1 else 's'}")
    return " ".join(parts)


class Throttle:
    """Only allow an action every `interval` seconds — used to keep progress
    message edits from hitting Telegram's rate limit during indexing."""

    def __init__(self, interval: float = 3.0):
        self.interval = interval
        self._last = 0.0

    def ready(self) -> bool:
        now = time.time()
        if now - self._last >= self.interval:
            self._last = now
            return True
        return False


_DURATION_UNITS = {
    "sec": 1, "second": 1, "seconds": 1,
    "min": 60, "minute": 60, "minutes": 60,
    "hour": 3600, "hours": 3600,
    "day": 86400, "days": 86400,
    "month": 30 * 86400, "months": 30 * 86400,
    "year": 365 * 86400, "years": 365 * 86400,
}


def parse_duration(text: str) -> int | None:
    """"1day" / "2 hours" / "1month" -> seconds. None if unparsable."""
    m = re.match(r"^\s*(\d+)\s*([a-zA-Z]+)\s*$", text or "")
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2).lower()
    multiplier = _DURATION_UNITS.get(unit)
    if not multiplier or value <= 0:
        return None
    return value * multiplier


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return "—"
    if len(value) <= keep:
        return "•" * len(value)
    return value[:keep] + "•" * (len(value) - keep)
