"""
HTTP side of Fast Download. Used by both the bot process (Koyeb/Render) and
the standalone Oracle server (stream_server.py).

  GET/HEAD /dl/<token>/<filename>   — file download, Range/resume supported
  GET      /health                  — lets the bot check this host is up
"""
import asyncio
import html
import logging
import re
from urllib.parse import quote, urlsplit

from aiohttp import web

from config import (
    STREAM_SECRET, STREAM_PREFETCH, STREAM_MAX_CONNECTIONS, STREAM_MAX_PER_LINK,
    STREAM_ONLY, STREAM_FALLBACK_URL,
)
from fastdl.pool import NotFound, pool
from fastdl.tokens import verify_token
from fastdl.usage import counter, HOST_KEY, _month

logger = logging.getLogger(__name__)

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_active_total = 0
_active_by_token: dict[str, int] = {}

_EXPIRED_HTML = (
    "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
    "<body style='font-family:sans-serif;text-align:center;padding:3em'>"
    "<h3>⏳ This download link has expired</h3>"
    "<p>Go back to the bot and request the file again to get a fresh link.</p></body>"
)


def _fallback_redirect(request: web.Request):
    """Oracle server only, and only if STREAM_FALLBACK_URL is set: when this server can't stream
    (its clients are down / Telegram unreachable) send the SAME path + token to the backup host.
    Tokens carry no host name, so the backup accepts them as they are."""
    if not (STREAM_ONLY and STREAM_FALLBACK_URL):
        return None
    if urlsplit(STREAM_FALLBACK_URL).netloc == request.host:  # never redirect to ourselves
        return None
    logger.warning("Redirecting %s to the fallback host", request.path[:60])
    return web.Response(status=302, headers={"Location": STREAM_FALLBACK_URL + request.raw_path,
                                             "Cache-Control": "no-store"})


def parse_range(header: str | None, size: int):
    """-> (start, end_inclusive, is_partial). Raises ValueError if unsatisfiable."""
    if not header:
        return 0, size - 1, False
    m = _RANGE_RE.match(header.strip())
    if not m:  # multi-range / garbage: legal to ignore and send the whole file
        return 0, size - 1, False
    a, b = m.groups()
    if a == "" and b == "":
        return 0, size - 1, False
    if a == "":  # suffix range: last N bytes
        n = int(b)
        if n == 0:
            raise ValueError("empty suffix")
        return max(size - n, 0), size - 1, True
    start = int(a)
    end = min(int(b), size - 1) if b else size - 1
    if start >= size or start > end:
        raise ValueError("out of range")
    return start, end, True


def _disposition(name: str, inline: bool = False) -> str:
    ascii_name = re.sub(r'[^\w.\- ]', "_", name.encode("ascii", "ignore").decode()) or "file"
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


async def handle_download(request: web.Request):
    return await _serve(request, inline=False)


async def handle_stream(request: web.Request):
    return await _serve(request, inline=True)


async def _serve(request: web.Request, inline: bool):
    global _active_total
    token = request.match_info["token"]
    parsed = verify_token(STREAM_SECRET, token)
    if not parsed:
        return web.Response(text=_EXPIRED_HTML, status=403, content_type="text/html")
    msg_id, _user_id, _exp = parsed

    if not pool.workers:
        return _fallback_redirect(request) or web.Response(
            text="Server is starting, try again in a moment.", status=503, headers={"Retry-After": "10"})
    try:
        meta = await pool.meta(msg_id)
    except NotFound:
        return web.Response(text="File not found.", status=404)
    except Exception:
        logger.exception("meta lookup failed for msg %s", msg_id)
        return _fallback_redirect(request) or web.Response(
            text="Couldn't reach Telegram, try again.", status=503, headers={"Retry-After": "5"})

    size = meta.size
    if size <= 0:
        return web.Response(text="File not found.", status=404)

    etag = f'"{msg_id}-{size}"'
    range_header = request.headers.get("Range")
    if_range = request.headers.get("If-Range")
    if if_range and if_range != etag:
        range_header = None  # validator mismatch: restart with the full file

    try:
        start, end, partial = parse_range(range_header, size)
    except ValueError:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})

    length = end - start + 1
    headers = {
        "Content-Type": (meta.mime if inline and meta.mime.startswith("video/") else "application/octet-stream"),
        "Content-Disposition": _disposition(meta.name, inline),
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    if request.method == "HEAD":
        headers["Content-Length"] = str(length)
        return web.Response(status=206 if partial else 200, headers=headers)

    if _active_total >= STREAM_MAX_CONNECTIONS or _active_by_token.get(token, 0) >= STREAM_MAX_PER_LINK:
        return web.Response(text="Server busy, try again in a minute.", status=429,
                            headers={"Retry-After": "15"})

    _active_total += 1
    _active_by_token[token] = _active_by_token.get(token, 0) + 1
    try:
        resp = web.StreamResponse(status=206 if partial else 200, headers=headers)
        resp.content_length = length
        await resp.prepare(request)
        try:
            async for data in pool.iter_range(msg_id, start, end, STREAM_PREFETCH):
                await resp.write(data)
                counter.record(len(data))  # real bytes sent -> monthly bandwidth counter
            await resp.write_eof()
        except (ConnectionResetError, ConnectionError):
            pass  # user closed the tab / paused the download — normal
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stream of msg %s aborted mid-way", msg_id)
            if request.transport:
                request.transport.close()  # truncated body -> client can resume
        return resp
    finally:
        _active_total -= 1
        left = _active_by_token.get(token, 1) - 1
        if left <= 0:
            _active_by_token.pop(token, None)
        else:
            _active_by_token[token] = left


_WATCH_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{margin:0;background:#0f0f10;color:#eee;font-family:system-ui,sans-serif}}
.w{{max-width:960px;margin:auto;padding:12px}}video{{width:100%;max-height:75vh;background:#000;border-radius:8px}}
h1{{font-size:15px;font-weight:600;word-break:break-word;margin:8px 0}}
.b{{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}}
.b a{{background:#2a2a2d;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-size:14px}}
p{{color:#aaa;font-size:13px;line-height:1.5}}</style></head><body><div class=w>
<h1>{title}</h1>
<video id=v controls playsinline preload=metadata src="{stream}"></video>
<div class=b>
<a href="{vlc}">Open in VLC</a>
<a href="{mx}">Open in MX Player</a>
<a href="{dl}">Download</a></div>
<p>If the video doesn't play here (common with MKV, HEVC/x265 or AC3/DTS audio), use
<b>Open in VLC</b> or <b>Open in MX Player</b> — they play almost every format and stream
the same link.</p></div></body></html>"""


async def handle_watch(request: web.Request):
    token = request.match_info["token"]
    if not verify_token(STREAM_SECRET, token):
        return web.Response(text=_EXPIRED_HTML, status=403, content_type="text/html")
    if not pool.workers:
        redirect = _fallback_redirect(request)
        if redirect:
            return redirect
    name = request.match_info.get("name") or "video"
    base = f"{request.scheme}://{request.host}"
    # Behind Caddy / Render / Koyeb the browser-facing scheme is https.
    if request.headers.get("X-Forwarded-Proto") == "https":
        base = f"https://{request.host}"
    qname = quote(name, safe="")
    stream = f"/stream/{token}/{qname}"
    full = f"{base}{stream}"
    mx = "intent:" + full.split("://", 1)[1] + "#Intent;scheme=" + full.split("://", 1)[0] + \
         ";package=com.mxtech.videoplayer.ad;type=video/*;end"
    html_page = _WATCH_HTML.format(
        title=html.escape(name), stream=stream, dl=f"/dl/{token}/{qname}",
        vlc=f"vlc://{full}", mx=html.escape(mx),
    )
    return web.Response(text=html_page, content_type="text/html",
                        headers={"Cache-Control": "private, no-store"})


async def handle_health(_request: web.Request):
    """The bot probes this on the Oracle host: 'workers' says whether it can stream right now,
    'bytes' is the real number of bytes it has sent this month."""
    try:
        used = await counter.month_bytes()
    except Exception:
        used = None
    body = {"ok": True, "host": HOST_KEY, "workers": len(pool.workers), "month": _month()}
    if used is not None:
        body["bytes"] = used
    return web.json_response(body)


def register_routes(app: web.Application):
    app.add_routes([
        web.get("/dl/{token}/{name:.*}", handle_download),
        web.get("/dl/{token}", handle_download),
        web.get("/stream/{token}/{name:.*}", handle_stream),
        web.get("/watch/{token}/{name:.*}", handle_watch),
        web.get("/health", handle_health),
    ])
