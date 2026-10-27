"""
Real bandwidth counter for THIS process's stream server.

Every byte written to a download/stream response is counted here (not the full
file size when the link is made), then saved in the background:

  * bot process (Koyeb/Render)  -> MongoDB, key "<month>:real:local"
  * Oracle stream_server.py     -> a small JSON file (STREAM_USAGE_FILE); Oracle has
                                   no database. The bot reads the number from
                                   Oracle's /health reply.

The month key is UTC, so the counter starts again from zero on the 1st.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from config import STREAM_ONLY, STREAM_USAGE_FILE

logger = logging.getLogger(__name__)

HOST_KEY = "oracle" if STREAM_ONLY else "local"   # which host this process is
DB_KEY = f"real:{HOST_KEY}"
FLUSH_EVERY = 15  # seconds


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class _Counter:
    def __init__(self):
        self._pending = 0          # bytes sent but not saved yet
        self._task = None
        self._file_month = _month()
        self._file_bytes = 0

    # ---- recording -------------------------------------------------------
    def record(self, nbytes: int):
        if nbytes > 0:
            self._pending += nbytes

    # ---- file mode (Oracle) ----------------------------------------------
    def _load_file(self):
        try:
            with open(STREAM_USAGE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("month") == _month():
                self._file_month, self._file_bytes = data["month"], int(data.get("bytes", 0))
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("Couldn't read %s — counting from zero", STREAM_USAGE_FILE, exc_info=True)

    def _roll_month(self):
        if self._file_month != _month():
            self._file_month, self._file_bytes = _month(), 0

    def _save_file(self):
        tmp = f"{STREAM_USAGE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"month": self._file_month, "bytes": self._file_bytes}, f)
        os.replace(tmp, STREAM_USAGE_FILE)

    # ---- saving ----------------------------------------------------------
    async def flush(self):
        n, self._pending = self._pending, 0
        if not n:
            return
        try:
            if STREAM_ONLY:
                self._roll_month()
                self._file_bytes += n
                self._save_file()
            else:
                from database.stream_db import add_usage  # bot process only (needs the DB)
                await add_usage(DB_KEY, n)
        except Exception:
            self._pending += n  # try again on the next round
            logger.warning("Couldn't save bandwidth usage — will retry", exc_info=True)

    async def _loop(self):
        while True:
            await asyncio.sleep(FLUSH_EVERY)
            await self.flush()

    async def start(self):
        if STREAM_ONLY:
            self._load_file()
        if not self._task:
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        await self.flush()

    # ---- reading ---------------------------------------------------------
    async def month_bytes(self) -> int:
        """Bytes sent this month by this process (saved + not yet saved)."""
        if STREAM_ONLY:
            self._roll_month()
            return self._file_bytes + self._pending
        from database.stream_db import get_usage
        return await get_usage(DB_KEY) + self._pending


counter = _Counter()
