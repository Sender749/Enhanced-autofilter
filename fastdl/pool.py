"""
Telegram client pool + fast chunk fetching.

Why not pyrogram's stream_media()? In this pyrofork version every get_file()
call opens a brand-new media connection (and re-authorizes on foreign DCs),
which is far too slow to do once per 1 MiB chunk. Here each client keeps ONE
long-lived media session per DC and sends many `upload.GetFile` requests over
it at the same time, spread across all clients in the pool.
"""
import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from pyrogram import Client, raw
from pyrogram.errors import AuthBytesInvalid, FileReferenceExpired, FileReferenceInvalid, FloodWait
from pyrogram.file_id import FileId
from pyrogram.session import Auth, Session

logger = logging.getLogger(__name__)

CHUNK = 1024 * 1024  # Telegram's max GetFile size; offsets stay 1 MiB-aligned
_MEDIA_KINDS = ("document", "video", "audio", "animation", "voice", "video_note")


class NotFound(Exception):
    """The BIN_CHANNEL message is gone or has no downloadable media."""


@dataclass
class Meta:
    size: int
    name: str
    mime: str


class _Worker:
    def __init__(self, client: Client, bin_channel: int, owned: bool = True):
        self.client = client
        self.bin_channel = bin_channel
        self.owned = owned  # False = the main bot's own client; the pool must never stop it
        self.active = 0
        self._sessions: dict[int, Session] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._locations: OrderedDict = OrderedDict()  # msg_id -> (location, dc, ts)

    # ── per-client file location (file_id is account-specific, so every
    #    client has to look the message up itself) ────────────────────────────
    async def locate(self, msg_id: int, force: bool = False):
        hit = self._locations.get(msg_id)
        if hit and not force and time.time() - hit[2] < 1200:
            return hit[0], hit[1], hit[3]

        msg = await self.client.get_messages(self.bin_channel, msg_id)
        media = None
        if msg and not msg.empty:
            for kind in _MEDIA_KINDS:
                media = getattr(msg, kind, None)
                if media:
                    break
        if not media:
            raise NotFound(f"no media in message {msg_id}")

        fid = FileId.decode(media.file_id)
        location = raw.types.InputDocumentFileLocation(
            id=fid.media_id,
            access_hash=fid.access_hash,
            file_reference=fid.file_reference,
            thumb_size=fid.thumbnail_size,
        )
        meta = Meta(
            size=int(media.file_size or 0),
            name=getattr(media, "file_name", None) or f"file_{msg_id}",
            mime=getattr(media, "mime_type", None) or "application/octet-stream",
        )
        self._locations[msg_id] = (location, fid.dc_id, time.time(), meta)
        while len(self._locations) > 500:
            self._locations.popitem(last=False)
        return location, fid.dc_id, meta

    async def _media_session(self, dc_id: int) -> Session:
        lock = self._locks.setdefault(dc_id, asyncio.Lock())
        async with lock:
            session = self._sessions.get(dc_id)
            if session:
                return session

            client = self.client
            test_mode = await client.storage.test_mode()
            own_dc = await client.storage.dc_id()
            if dc_id != own_dc:
                auth_key = await Auth(client, dc_id, test_mode).create()
            else:
                auth_key = await client.storage.auth_key()

            session = Session(client, dc_id, auth_key, test_mode, is_media=True)
            await session.start()
            if dc_id != own_dc:
                for _ in range(3):
                    try:
                        exported = await client.invoke(raw.functions.auth.ExportAuthorization(dc_id=dc_id))
                        await session.invoke(
                            raw.functions.auth.ImportAuthorization(id=exported.id, bytes=exported.bytes)
                        )
                        break
                    except AuthBytesInvalid:
                        continue
                else:
                    await session.stop()
                    raise RuntimeError(f"couldn't authorize media session on DC {dc_id}")
            self._sessions[dc_id] = session
            return session

    async def _drop_session(self, dc_id: int):
        session = self._sessions.pop(dc_id, None)
        if session:
            try:
                await session.stop()
            except Exception:
                pass

    async def read(self, msg_id: int, index: int) -> bytes:
        self.active += 1
        try:
            last_exc = None
            for attempt in range(3):
                try:
                    location, dc_id, _ = await self.locate(msg_id, force=attempt > 0 and isinstance(last_exc, (FileReferenceExpired, FileReferenceInvalid)))
                    session = await self._media_session(dc_id)
                    result = await session.invoke(
                        raw.functions.upload.GetFile(location=location, offset=index * CHUNK, limit=CHUNK),
                        sleep_threshold=30,
                    )
                    if isinstance(result, raw.types.upload.File):
                        return result.bytes
                    raise RuntimeError("CDN redirect responses aren't supported")
                except NotFound:
                    raise
                except (FileReferenceExpired, FileReferenceInvalid) as e:
                    last_exc = e
                    self._locations.pop(msg_id, None)
                except Exception as e:
                    last_exc = e
                    logger.warning("chunk %s of msg %s failed (try %s): %r", index, msg_id, attempt + 1, e)
                    # Connection may be dead — rebuild the media session next try.
                    for dc in list(self._sessions):
                        await self._drop_session(dc)
                    await asyncio.sleep(0.5 * (attempt + 1))
            raise last_exc
        finally:
            self.active -= 1

    async def stop(self):
        for dc in list(self._sessions):
            await self._drop_session(dc)
        if not self.owned:
            return  # main bot client: the bot itself stops it
        try:
            await self.client.stop()
        except Exception:
            pass


class StreamPool:
    def __init__(self):
        self.workers: list[_Worker] = []
        self._meta: OrderedDict = OrderedDict()  # msg_id -> (Meta, ts)
        self._cfg = None            # (api_id, api_hash, tokens, bin_channel) from the last start()
        self._main = None           # the main bot client, if add_main_client() was used
        self._recover_lock = asyncio.Lock()
        self._last_recover = 0.0

    def add_main_client(self, client: Client, bin_channel: int):
        """Use the ALREADY-RUNNING main bot client as a stream worker. No second
        login with the same bot token is needed, so this can never fail to start
        (a second session of the same token can be refused/flood-waited, which
        used to leave the pool empty and every link request 'busy')."""
        self._main = (client, bin_channel)
        if not any(w.client is client for w in self.workers):
            self.workers.insert(0, _Worker(client, bin_channel, owned=False))
            logger.info("Fast-download pool: main bot client added as a stream worker")

    async def _start_one(self, i: int, api_id: int, api_hash: str, token: str, bin_channel: int):
        """Start one helper client; retries a couple of times (flood-waits, slow network)."""
        last = None
        for attempt in range(3):
            client = Client(
                f"fastdl_{i}",
                api_id=api_id,
                api_hash=api_hash,
                bot_token=token,
                in_memory=True,   # nothing written to disk — fine on ephemeral hosts
                no_updates=True,  # these sessions only download, never handle updates
                workers=1,
                sleep_threshold=30,
                max_concurrent_transmissions=8,
            )
            try:
                await client.start()
                return _Worker(client, bin_channel)
            except FloodWait as e:
                last = e
                wait = int(getattr(e, "value", 5) or 5)
                logger.warning("Stream client #%s flood-waited %ss (try %s)", i, wait, attempt + 1)
                await asyncio.sleep(min(wait, 30))
            except Exception as e:
                last = e
                logger.warning("Stream client #%s failed to start (try %s): %r", i, attempt + 1, e)
                await asyncio.sleep(2 * (attempt + 1))
        logger.error("Stream client #%s could not start — skipping it (%r)", i, last)
        return None

    async def start(self, api_id: int, api_hash: str, tokens: list, bin_channel: int):
        self._cfg = (api_id, api_hash, list(tokens), bin_channel)
        seen = {w.client.bot_token for w in self.workers if getattr(w.client, "bot_token", None)}
        for i, token in enumerate(tokens):
            if not token or token in seen:
                continue
            seen.add(token)
            worker = await self._start_one(i, api_id, api_hash, token, bin_channel)
            if worker:
                self.workers.append(worker)
        logger.info("Fast-download pool ready with %s client(s)", len(self.workers))

    async def ensure_workers(self) -> bool:
        """Called when a link is requested and the pool is empty: try to bring
        it back (at most once every 20 s) instead of failing every request."""
        if self.workers:
            return True
        async with self._recover_lock:
            if self.workers:
                return True
            if time.time() - self._last_recover < 20:
                return False
            self._last_recover = time.time()
            logger.warning("Stream pool is empty — trying to bring it back")
            if self._main:
                self.add_main_client(*self._main)
            if not self.workers and self._cfg:
                await self.start(*self._cfg)
            return bool(self.workers)

    async def stop(self):
        for w in self.workers:
            await w.stop()
        self.workers.clear()

    # ── metadata (size / name / mime) — same for every client, cached ────────
    async def meta(self, msg_id: int) -> Meta:
        hit = self._meta.get(msg_id)
        if hit and time.time() - hit[1] < 3600:
            return hit[0]
        if not self.workers:
            raise RuntimeError("no stream clients available")
        worker = min(self.workers, key=lambda w: w.active)
        _, _, meta = await worker.locate(msg_id)
        self._meta[msg_id] = (meta, time.time())
        while len(self._meta) > 2000:
            self._meta.popitem(last=False)
        return meta

    async def _read(self, msg_id: int, index: int) -> bytes:
        """One chunk, from the least-busy client; fails over to the others."""
        order = sorted(self.workers, key=lambda w: w.active)
        last_exc = None
        for worker in order[:3]:
            try:
                return await worker.read(msg_id, index)
            except NotFound:
                raise
            except Exception as e:
                last_exc = e
        raise last_exc or RuntimeError("no stream clients available")

    async def iter_range(self, msg_id: int, start: int, end: int, window: int = 4):
        """Yield bytes [start, end] (inclusive) in order, keeping `window`
        chunk requests in flight at once. This overlap is what turns a
        latency-bound ~1-3 MB/s into the host's full speed."""
        first, last = start // CHUNK, end // CHUNK
        pending: list = []
        nxt = first
        try:
            while nxt <= last or pending:
                while nxt <= last and len(pending) < window:
                    pending.append((nxt, asyncio.ensure_future(self._read(msg_id, nxt))))
                    nxt += 1
                index, task = pending.pop(0)
                data = await task
                lo = start - index * CHUNK if index == first else 0
                hi = end - index * CHUNK + 1 if index == last else len(data)
                yield data[max(lo, 0):hi]
        finally:
            for _, task in pending:
                task.cancel()


pool = StreamPool()
