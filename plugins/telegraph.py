"""
/telegraph  (also /telepragh, /tgraph) — ADMIN ONLY.

Turns a photo / video into a public link, and also shows the Telegram
`file_id` of the same file. Use it to fill config.py:

    PREMIUM_PHOTO = <link or file_id of a photo>
    WELCOME_VIDEO = <file_id of the how-it-works video>

How to use (any of these):
    1. Reply to a photo / video with /telegraph
    2. Send a photo / video with /telegraph as its caption
    3. Send /telegraph, then send the photo / video

Providers (config.TELEGRAPH_PROVIDER): "auto" tries telegra.ph / graph.org (files up
to 5 MB), then Catbox (200 MB), envs.sh, 0x0.st and finally Litterbox (72-hour
temporary). If every host fails, the reply lists what each one answered.
"""
import asyncio
import html
import logging
import mimetypes
import os
import time

import aiohttp
from pyrogram import Client, filters
from pyrogram.errors import ListenerTimeout, ListenerStopped
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMINS, TELEGRAPH_PROVIDER
from utils import human_size
from strings import (
    TELEGRAPH_PROMPT_TXT, TELEGRAPH_TIMEOUT_TXT, TELEGRAPH_NOT_MEDIA_TXT,
    TELEGRAPH_TOO_BIG_TXT, TELEGRAPH_DOWNLOADING_TXT, TELEGRAPH_UPLOADING_TXT,
    TELEGRAPH_FAILED_TXT, TELEGRAPH_DONE_TXT, TELEGRAPH_CANCEL_BTN, TELEGRAPH_TEMP_NOTE,
)

logger = logging.getLogger(__name__)

TELEGRAPH_LIMIT = 5 * 1024 * 1024        # telegra.ph / graph.org
CATBOX_LIMIT = 200 * 1024 * 1024         # catbox.moe
LITTERBOX_LIMIT = 1024 * 1024 * 1024     # litterbox (temporary)
_BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_CURL_UA = "curl/8.5.0"                  # 0x0.st / envs.sh refuse browser-like and library UAs

_PENDING: set = set()                    # (chat_id, user_id) while waiting for media


# ── what counts as uploadable media ──────────────────────────────────────────

def _media_of(message):
    """(media_object, kind) for a photo / video / animation / image-or-video
    document, else None."""
    if not message:
        return None
    if message.photo:
        return message.photo, "photo"
    if message.video:
        return message.video, "video"
    if message.animation:
        return message.animation, "video"
    doc = message.document
    if doc and (doc.mime_type or "").startswith(("image/", "video/")):
        return doc, "video" if doc.mime_type.startswith("video/") else "photo"
    return None


def _ext_for(media, kind: str) -> str:
    name = getattr(media, "file_name", None) or ""
    if "." in name:
        return os.path.splitext(name)[1].lower()
    return ".jpg" if kind == "photo" else ".mp4"


# ── providers ────────────────────────────────────────────────────────────────
# Every host is tried in turn until one gives back a link. Hosts that keep files
# only for a while are flagged in _TEMP_HOSTS so the admin is told.

_TEMP_HOSTS = {"litterbox.catbox.moe": "72 hours"}


def _timeout_for(size: int) -> aiohttp.ClientTimeout:
    # 2 min base + ~3 s per MB, never more than 15 min
    return aiohttp.ClientTimeout(total=min(900, 120 + (size // (1024 * 1024)) * 3), connect=15)


async def _post_file(url: str, path: str, field: str, *, extra=None, ua=_BROWSER_UA, size=0):
    """multipart POST of one file. Returns (http_status, response_text).
    The file part carries a REAL Content-Type (image/jpeg, video/mp4 …) — telegra.ph
    answers "File type invalid" to the generic application/octet-stream."""
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    async with aiohttp.ClientSession(timeout=_timeout_for(size), headers={"User-Agent": ua}) as session:
        with open(path, "rb") as fh:
            form = aiohttp.FormData()
            for k, v in (extra or {}).items():
                form.add_field(k, v)
            form.add_field(field, fh, filename=os.path.basename(path), content_type=ctype)
            async with session.post(url, data=form) as resp:
                return resp.status, (await resp.text()).strip()


def _brief(status: int, text: str) -> str:
    text = " ".join((text or "").split())
    return f"HTTP {status}: {text[:110] or 'empty reply'}"


async def _upload_telegraph(path: str, host: str, size: int):
    """telegra.ph-style: POST /upload -> [{"src": "/file/xxxx.jpg"}] or {"error": ...}"""
    import json
    status, text = await _post_file(f"https://{host}/upload", path, "file", size=size)
    try:
        data = json.loads(text)
    except ValueError:
        raise RuntimeError(_brief(status, text))
    if isinstance(data, list) and data and data[0].get("src"):
        return f"https://{host}{data[0]['src']}"
    raise RuntimeError(_brief(status, text))


async def _upload_plain(url: str, path: str, field: str, size: int, *, extra=None, ua=_BROWSER_UA):
    """Hosts that answer with the bare file URL as plain text."""
    status, text = await _post_file(url, path, field, extra=extra, ua=ua, size=size)
    link = text.splitlines()[0].strip() if text else ""
    if status == 200 and link.startswith("http"):
        return link
    raise RuntimeError(_brief(status, text))


def _providers(path: str, size: int):
    """[(host, coroutine-factory)] in the order they should be tried."""
    mode = TELEGRAPH_PROVIDER
    out = []
    if mode in ("auto", "telegraph") and size <= TELEGRAPH_LIMIT:
        out += [("telegra.ph", lambda: _upload_telegraph(path, "telegra.ph", size)),
                ("graph.org", lambda: _upload_telegraph(path, "graph.org", size))]
    if mode in ("auto", "catbox"):
        if size <= CATBOX_LIMIT:
            out.append(("catbox.moe", lambda: _upload_plain(
                "https://catbox.moe/user/api.php", path, "fileToUpload", size,
                extra={"reqtype": "fileupload"})))
        if mode == "auto":
            out.append(("envs.sh", lambda: _upload_plain(
                "https://envs.sh", path, "file", size, ua=_CURL_UA)))
            out.append(("0x0.st", lambda: _upload_plain(
                "https://0x0.st", path, "file", size, ua=_CURL_UA)))
            if size <= LITTERBOX_LIMIT:
                out.append(("litterbox.catbox.moe", lambda: _upload_plain(
                    "https://litterbox.catbox.moe/resources/internals/api.php", path, "fileToUpload", size,
                    extra={"reqtype": "fileupload", "time": "72h"})))
    return out


async def upload_file(path: str, size: int):
    """-> (url, host). Raises RuntimeError listing what every provider answered."""
    errors = []
    for host, make in _providers(path, size):
        try:
            return await make(), host
        except Exception as exc:                       # network error, timeout, bad reply …
            reason = str(exc) or type(exc).__name__
            logger.warning("Upload to %s failed: %s", host, reason)
            errors.append(f"{host} — {reason[:140]}")
    raise RuntimeError("\n".join(errors) or "no provider accepts a file this size")


def _limit_for_provider() -> int:
    return TELEGRAPH_LIMIT if TELEGRAPH_PROVIDER == "telegraph" else LITTERBOX_LIMIT


# ── command ──────────────────────────────────────────────────────────────────

@Client.on_message(filters.command(["telegraph", "telepragh", "tgraph"]) & filters.user(ADMINS))
async def telegraph_cmd(bot, message):
    source = message.reply_to_message if _media_of(message.reply_to_message) else (
        message if _media_of(message) else None
    )

    status = None
    if source is None:
        # Nothing attached — ask for it (with a Cancel button).
        status = await message.reply_text(
            TELEGRAPH_PROMPT_TXT, quote=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(TELEGRAPH_CANCEL_BTN, callback_data="tgph#cancel")]]),
        )
        key = (message.chat.id, message.from_user.id)
        _PENDING.add(key)
        try:
            reply = await bot.listen(chat_id=key[0], user_id=key[1], timeout=60)
        except ListenerTimeout:
            await status.edit_text(TELEGRAPH_TIMEOUT_TXT)
            return
        except ListenerStopped:                        # Cancel pressed (message already removed)
            return
        finally:
            _PENDING.discard(key)
        if not _media_of(reply):
            await status.edit_text(TELEGRAPH_NOT_MEDIA_TXT)
            return
        source = reply

    media, kind = _media_of(source)
    size = getattr(media, "file_size", 0) or 0
    limit = _limit_for_provider()
    if size > limit:
        text = TELEGRAPH_TOO_BIG_TXT.format(size=human_size(size), limit=human_size(limit))
        await (status.edit_text(text) if status else message.reply_text(text, quote=True))
        return

    if status:
        await status.edit_text(TELEGRAPH_DOWNLOADING_TXT)
    else:
        status = await message.reply_text(TELEGRAPH_DOWNLOADING_TXT, quote=True)

    # Download to disk (not RAM) so a 100 MB video can't blow a small instance.
    path = f"/tmp/tg_{int(time.time())}_{message.from_user.id}{_ext_for(media, kind)}"
    try:
        downloaded = await bot.download_media(source, file_name=path)
        if isinstance(downloaded, str):
            path = downloaded
        real_size = os.path.getsize(path)

        await status.edit_text(TELEGRAPH_UPLOADING_TXT)
        try:
            url, host = await upload_file(path, real_size)
        except RuntimeError as exc:
            await status.edit_text(TELEGRAPH_FAILED_TXT.format(error=html.escape(str(exc))))
            return
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open Link", url=url)],
        [InlineKeyboardButton("❌ Close", callback_data="tgph#close")],
    ])
    await status.edit_text(
        TELEGRAPH_DONE_TXT.format(
            url=url, host=host, file_id=media.file_id,
            note=TELEGRAPH_TEMP_NOTE.format(keep=_TEMP_HOSTS[host]) if host in _TEMP_HOSTS else "",
        ),
        reply_markup=markup,
        disable_web_page_preview=True,
    )


@Client.on_callback_query(filters.regex(r"^tgph#close$") & filters.user(ADMINS))
async def telegraph_close(_, query):
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass


@Client.on_callback_query(filters.regex(r"^tgph#cancel$") & filters.user(ADMINS))
async def telegraph_cancel(bot, query):
    await query.answer()
    key = (query.message.chat.id, query.from_user.id)
    if key in _PENDING:
        await bot.stop_listening(chat_id=key[0], user_id=key[1])
    try:
        await query.message.delete()
    except Exception:
        pass
