"""
/stats — the admin dashboard.

Works like the /settings panel: one home screen with live numbers and a grid of
buttons; every button opens a full detail page with a Back button.

    👥 Users          totals, new users, DAU / WAU / MAU, retention
    🔥 Activity       daily active users, who requested files (and which files), busiest hours
    📁 Files & Index  files indexed per day / per channel, library by language & quality
    🔗 Shortener      who completed which shortener (1 / 2 / 3) each day
    🔎 Search         searches, found-rate, top searches, top misses
    📝 Requests       /req requests, status, most asked titles
    💎 Premium        active plans, expiring soon
    🏘 Groups         groups, new / left, busiest groups
    📣 Ads & Sponsors audience numbers sponsors ask for + shareable summary + CSV export
    📅 Daily Report   one table, last 7 / 14 / 30 days
    🖥 Server         uptime, RAM, CPU, disk, latency, feature switches, fast-download usage
    🗄 Database       storage, biggest collections

Per-day activity is recorded by database/stats_db.py (see its docstring).
Admin-only, and only in the bot's private chat (it shows user ids and names).
"""
import asyncio
import csv
import html
import io
import logging
import os
import platform
import time
from datetime import datetime, timedelta, timezone

import pyrogram
from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from pyrogram.types import InlineKeyboardMarkup as Markup, InlineKeyboardButton as Btn

from config import ADMINS
from database import stats_db as S
from database.client import db
from database.filters_db import files
from database.premium_db import premium_col
from database.request_db import request_col
from database.settings_db import get_settings
from utils import IST, readable_time

try:
    import psutil
except ImportError:                      # the panel still works, the Server page just shows less
    psutil = None

logger = logging.getLogger(__name__)

ADMIN = filters.user(ADMINS)
esc = html.escape

_PROC_START = time.time()
# Atlas free (M0) holds 512 MB. Set MONGO_STORAGE_LIMIT_MB if your plan is different.
_DB_LIMIT_MB = float(os.environ.get("MONGO_STORAGE_LIMIT_MB", "512"))
# Optional: what 1,000 shortener completions earn you. 0 = don't show earnings.
_EARN_PER_1000 = float(os.environ.get("SHORTENER_EARN_PER_1000", "0") or 0)
_CURRENCY = os.environ.get("STATS_CURRENCY", "₹")

_PAGE = 5            # people per page on lists
_PERIODS = [("t", "Today"), ("y", "Yday"), ("w", "7 Days"), ("m", "30 Days")]
_LANGS = ["hindi", "punjabi", "english", "tamil", "telugu", "malayalam", "kannada", "bengali", "marathi",
          "gujarati", "urdu", "bhojpuri", "korean", "japanese", "chinese", "spanish", "french"]
_QUALITIES = ["2160p", "1080p", "720p", "480p", "360p"]


# ══════════════════════════════════════════════════════════════════════════════
# SMALL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def n_(v) -> str:
    return f"{int(v or 0):,}"


def pct(a, b) -> str:
    return f"{100 * a / b:.0f}%" if b else "—"


def bar(done, total, width: int = 10) -> str:
    filled = int(width * done / total) if total else 0
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def spark(values: list) -> str:
    """A tiny trend line: ▁▂▃▄▅▆▇█"""
    blocks = "▁▂▃▄▅▆▇█"
    top = max(values) if values else 0
    if top <= 0:
        return "▁" * len(values)
    return "".join(blocks[min(7, int(v / top * 7.999))] for v in values)


def k_(v) -> str:
    v = int(v or 0)
    return str(v) if v < 1000 else (f"{v / 1000:.1f}k" if v < 100000 else f"{v // 1000}k")


def d_label(day: str) -> str:
    return datetime.strptime(day, "%Y-%m-%d").strftime("%d %b")


def utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def t_label(dt) -> str:
    return utc(dt).astimezone(IST).strftime("%I:%M %p") if dt else "—"


def dt_label(dt) -> str:
    return utc(dt).astimezone(IST).strftime("%d %b, %I:%M %p") if dt else "—"


def person(uid: int, name: str | None, username: str | None) -> str:
    out = f'<a href="tg://user?id={uid}">{esc(name or f"User {uid}")}</a>'
    if username:
        out += f' (<a href="https://t.me/{esc(username)}">@{esc(username)}</a>)'
    return out


def group_name(doc: dict) -> str:
    title = esc(doc.get("title") or str(doc.get("_id")))
    href = f"https://t.me/{doc['username']}" if doc.get("username") else doc.get("invite_link")
    return f'<a href="{href}">{title}</a>' if href else f"<b>{title}</b>"


def hour_label(h: int) -> str:
    return datetime(2000, 1, 1, h).strftime("%I %p").lstrip("0")


def clip(text: str, limit: int = 3900) -> str:
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"


_cache: dict = {}


async def cached(key, ttl: int, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = await fn()
    _cache[key] = (time.time(), val)
    return val


def period_days(code: str) -> list:
    return {"y": lambda: [S.ist_day(-1)], "w": lambda: S.days_back(7), "m": lambda: S.days_back(30)}.get(
        code, lambda: [S.ist_day()])()


def period_title(code: str) -> str:
    return {"t": f"Today · {d_label(S.ist_day())}", "y": f"Yesterday · {d_label(S.ist_day(-1))}",
            "w": "Last 7 days", "m": "Last 30 days"}.get(code, "Today")


def period_row(page: str, code: str, tail: str = "") -> list:
    return [Btn(("✅ " if c == code else "") + label, callback_data=f"st#{page}#{c}" + (f"#{tail}" if tail else ""))
            for c, label in _PERIODS]


def list_nav(prefix: str, page: int, total: int, per: int) -> list:
    """◀️ Prev · 2/5 · Next ▶️ — `prefix` is the callback up to the page number."""
    pages = max(1, (total + per - 1) // per)
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(Btn("◀️ Prev", callback_data=f"{prefix}#{page - 1}"))
    row.append(Btn(f"{page + 1}/{pages}", callback_data="st#noop"))
    if page < pages - 1:
        row.append(Btn("Next ▶️", callback_data=f"{prefix}#{page + 1}"))
    return [row]


def nav_row(extra: list | None = None) -> list:
    return (extra or []) + [Btn("⬅️ Back", callback_data="st#home"), Btn("🔄 Refresh", callback_data="st#rf")]


async def safe_edit(msg, text, markup=None):
    try:
        await msg.edit_text(clip(text), reply_markup=markup, disable_web_page_preview=True)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(min(e.value, 5))
    except RPCError:
        logger.debug("stats edit failed", exc_info=True)


async def user_names(uids: list) -> dict:
    out = {}
    if uids:
        async for d in db["bot_users"].find({"_id": {"$in": list(uids)}}, {"name": 1, "username": 1}):
            out[d["_id"]] = d
    return out


# ══════════════════════════════════════════════════════════════════════════════
# NUMBERS SHARED BY SEVERAL PAGES
# ══════════════════════════════════════════════════════════════════════════════

async def totals() -> dict:
    async def load():
        now = datetime.now(timezone.utc)
        users, seen, blocked, groups, left, banned, prem, nfiles = await asyncio.gather(
            db["users"].estimated_document_count(),
            db["bot_users"].count_documents({}),
            db["bot_users"].count_documents({"active": False}),
            db["bot_groups"].count_documents({"left": {"$ne": True}}),
            db["bot_groups"].count_documents({"left": True}),
            db["banned_users"].count_documents({}),
            premium_col.count_documents({"expiry_time": {"$gt": now}}),
            files.estimated_document_count(),
        )
        return {"users": users, "seen": seen, "blocked": blocked, "groups": groups, "left": left,
                "banned": banned, "premium": prem, "files": nfiles}
    return await cached("totals", 30, load)


async def db_usage() -> dict:
    async def load():
        try:
            st = await db.command("dbStats", scale=1)
        except Exception:
            return {}
        used = st.get("storageSize", 0) + st.get("indexSize", 0)
        limit = _DB_LIMIT_MB * 1024 * 1024
        return {"used": used, "limit": limit, "pct": used / limit * 100 if limit else 0, "data": st.get("dataSize", 0),
                "index": st.get("indexSize", 0), "collections": st.get("collections", 0), "objects": st.get("objects", 0)}
    return await cached("dbstats", 300, load)


def mb(n: float) -> str:
    n /= 1024 * 1024
    return f"{n / 1024:.2f} GB" if n >= 1024 else f"{n:.1f} MB"


def dot(pct_used: float) -> str:
    return "🟢" if pct_used < 70 else ("🟡" if pct_used < 90 else "🔴")


async def requests_count(days: list) -> dict:
    a, _ = S.day_bounds(days[0])
    _, b = S.day_bounds(days[-1])
    match = {"created_at": {"$gte": a, "$lt": b}}
    by_status = {}
    async for r in request_col.aggregate([{"$match": match}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        by_status[r["_id"] or "pending"] = int(r["n"])
    users = 0
    async for r in request_col.aggregate([{"$match": match}, {"$group": {"_id": "$user_id"}}, {"$count": "n"}]):
        users = r["n"]
    return {"total": sum(by_status.values()), "users": int(users), "by_status": by_status, "match": match}


def lang_counts(rows: list) -> list:
    """Languages mentioned in searches, weighted by how often each query was searched."""
    counts: dict = {}
    for r in rows:
        words = set((r.get("_id") or "").split())
        for lang in _LANGS:
            if lang in words:
                counts[lang] = counts.get(lang, 0) + int(r["n"])
    return sorted(counts.items(), key=lambda kv: -kv[1])


# ══════════════════════════════════════════════════════════════════════════════
# PAGES — each returns (text, markup)
# ══════════════════════════════════════════════════════════════════════════════

async def page_home(bot):
    today, yday = [S.ist_day()], [S.ist_day(-1)]
    settings = await get_settings()
    a_t, a_y, v_t, idx, ss, rq, tot, dbu = await asyncio.gather(
        S.activity_totals(today), S.activity_totals(yday), S.verify_summary(today),
        S.indexed_per_day(yday + today), S.search_summary(today), requests_count(today), totals(), db_usage())
    done = {t: v_t[t]["done"] for t in (1, 2, 3)}
    flags = [("Autofilter", settings.get("autofilter_enabled", True)), ("PM filter", settings.get("pm_filter_enabled", True)),
             ("Verify", settings.get("verify_enabled")), ("Limit", settings.get("file_limit_enabled")),
             ("Premium", settings.get("premium_enabled")), ("F-Sub", settings.get("force_sub_enabled"))]
    up = readable_time(time.time() - (psutil.Process(os.getpid()).create_time() if psutil else _PROC_START))
    now = datetime.now(timezone.utc).astimezone(IST).strftime("%d %b %Y, %I:%M %p")

    text = (
        f"📊 <b>Bot Dashboard</b>\n<i>{now} IST</i>\n\n"
        + " · ".join(f"{'🟢' if on else '🔴'} {name}" for name, on in flags) + "\n\n"
        f"<b>Today</b>  <i>(yesterday)</i>\n"
        f"👥 Active users: <b>{n_(a_t['users'])}</b>  <i>({n_(a_y['users'])})</i>\n"
        f"📥 File requesters: <b>{n_(a_t['req'])}</b> · files sent <b>{n_(a_t['files'])}</b>  <i>({n_(a_y['files'])})</i>\n"
        f"🔗 Shortener done: <b>{n_(sum(done.values()))}</b>  "
        f"<i>(S1 {done[1]} · S2 {done[2]} · S3 {done[3]})</i>\n"
        f"📚 Indexed: <b>{n_(idx[today[0]])}</b>  <i>({n_(idx[yday[0]])})</i>\n"
        f"🔎 Searches: <b>{n_(ss['total'])}</b> · found <b>{pct(ss['hits'], ss['total'])}</b>\n"
        f"📝 Requests: <b>{n_(rq['total'])}</b>\n\n"
        f"<b>Overall</b>\n"
        f"👥 <b>{n_(tot['users'])}</b> users · 🏘 <b>{n_(tot['groups'])}</b> groups · "
        f"💎 <b>{n_(tot['premium'])}</b> premium · 📁 <b>{n_(tot['files'])}</b> files\n"
        + (f"🗄 Database <b>{dbu['pct']:.0f}%</b> used {dot(dbu['pct'])} · " if dbu else "")
        + f"⏱ Up <b>{up}</b>\n\n<i>Tap a button for the full details 👇</i>"
    )
    rows = [
        [Btn("👥 Users", callback_data="st#users"), Btn("🔥 Activity", callback_data="st#act#t#0")],
        [Btn("📁 Files & Index", callback_data="st#files"), Btn("🔗 Shortener", callback_data="st#short#t#a#0")],
        [Btn("🔎 Search", callback_data="st#search#t"), Btn("📝 Requests", callback_data="st#req#t")],
        [Btn("💎 Premium", callback_data="st#prem"), Btn("🏘 Groups", callback_data="st#grp#t")],
        [Btn("📣 Ads & Sponsors", callback_data="st#ads"), Btn("📅 Daily Report", callback_data="st#rep#7")],
        [Btn("🖥 Server", callback_data="st#srv"), Btn("🗄 Database", callback_data="st#dbs")],
        [Btn("🔄 Refresh", callback_data="st#rf"), Btn("✖️ Close", callback_data="st#close")],
    ]
    return text, Markup(rows)


async def page_users(bot):
    d14, d30 = S.days_back(14), S.days_back(30)
    today = [S.ist_day()]
    tot, new30, a_d, a_w, a_m, ret = await asyncio.gather(
        totals(), S.new_users_per_day(d30), S.activity_totals(today), S.activity_totals(S.days_back(7)),
        S.activity_totals(d30), S.retention())
    new14 = [new30[d] for d in d14]
    last7 = sum(new30[d] for d in d30[-7:])
    reach = tot["seen"] - tot["blocked"]
    stick = pct(a_d["users"], a_m["users"])
    text = (
        "👥 <b>Users</b>\n\n"
        f"<b>Total users:</b> <code>{n_(tot['users'])}</code> <i>(started the bot)</i>\n"
        f"📨 Reachable by broadcast: <code>{n_(reach)}</code>\n"
        f"🚫 Blocked / inactive: <code>{n_(tot['blocked'])}</code> · Banned: <code>{n_(tot['banned'])}</code>\n"
        f"💎 Premium: <code>{n_(tot['premium'])}</code> ({pct(tot['premium'], tot['users'])})\n\n"
        "<b>🆕 New users</b>\n"
        f"Today <b>{n_(new30[d30[-1]])}</b> · Yesterday <b>{n_(new30[d30[-2]])}</b> · "
        f"7 days <b>{n_(last7)}</b> · 30 days <b>{n_(sum(new30.values()))}</b>\n"
        f"<code>{spark(new14)}</code>  <i>last 14 days</i>\n\n"
        "<b>🔥 Active users</b>\n"
        f"Today (DAU) <b>{n_(a_d['users'])}</b> · 7 days (WAU) <b>{n_(a_w['users'])}</b> · 30 days (MAU) <b>{n_(a_m['users'])}</b>\n"
        f"Stickiness (DAU ÷ MAU): <b>{stick}</b>\n"
    )
    if ret["cohort"]:
        text += (f"\n<b>🔁 Retention</b>\n{pct(ret['back'], ret['cohort'])} of the <code>{n_(ret['cohort'])}</code> users who joined "
                 f"8–14 days ago were active in the last 7 days.\n")
    since = await S.tracking_since()
    if since:
        text += f"\n<i>ℹ️ Daily tracking started on {d_label(since)}; active-user numbers before that day are not available.</i>"
    return text, Markup([nav_row()])


async def page_activity(bot, code: str, page: int):
    days = period_days(code)
    total_days = await S.activity_totals(days)
    series = await S.daily_series(days)
    hours = S.hours_total(series)
    rows, nreq = await S.requesters(days, page * _PAGE, _PAGE)
    new_users = await S.new_users_per_day(days)
    peak = max(range(24), key=lambda h: hours[h]) if any(hours) else None

    text = (
        f"🔥 <b>Activity — {period_title(code)}</b>\n\n"
        f"👥 Active users: <b>{n_(total_days['users'])}</b>  <i>(PM {n_(total_days['pm'])} · groups {n_(total_days['grp'])})</i>\n"
        f"💬 Messages & taps: <b>{n_(total_days['msgs'])}</b>\n"
        f"🆕 New users: <b>{n_(sum(new_users.values()))}</b>\n"
        f"📥 File requesters: <b>{n_(total_days['req'])}</b> ({pct(total_days['req'], total_days['users'])} of active)\n"
        f"📄 Files sent: <b>{n_(total_days['files'])}</b>"
        + (f" · avg <b>{total_days['files'] / total_days['req']:.1f}</b> per requester" if total_days["req"] else "") + "\n"
    )
    if peak is not None:
        text += f"⏰ Busiest hour: <b>{hour_label(peak)}–{hour_label((peak + 1) % 24)}</b> IST\n"
        blocks = [sum(hours[i:i + 3]) for i in range(0, 24, 3)]
        text += f"<code>{spark(blocks)}</code>  <i>12am → 12am, 3-hour blocks</i>\n"
    text += f"\n<b>📥 Users who requested files</b>  <i>({n_(nreq)})</i>\n"
    if not rows:
        text += "<i>No file requests in this period.</i>\n"
    names = await user_names([r["_id"] for r in rows])
    for i, r in enumerate(rows, start=page * _PAGE + 1):
        nm = r.get("nm") or (names.get(r["_id"]) or {}).get("name")
        un = r.get("un") or (names.get(r["_id"]) or {}).get("username")
        text += f"\n<b>{i}.</b> {person(r['_id'], nm, un)}\n    🆔 <code>{r['_id']}</code> · 📥 <b>{n_(r['f'])}</b> file(s)\n"
        for f in r["files"][-3:]:
            text += f"    📄 <code>{esc(f[:48])}</code>\n"
    since = await S.tracking_since()
    if since and days[0] < since:
        text += f"\n<i>ℹ️ Tracking started on {d_label(since)}, so earlier days show 0.</i>"
    markup = [period_row("act", code, "0"), *list_nav(f"st#act#{code}", page, nreq, _PAGE), nav_row()]
    return text, Markup(markup)


async def page_files(bot):
    settings = await get_settings()
    d14 = S.days_back(14)
    today, yday = d14[-1], d14[-2]

    async def library():
        langs = await asyncio.gather(*(files.count_documents({"words": w}) for w in _LANGS))
        quals = await asyncio.gather(*(files.count_documents({"words": w}) for w in _QUALITIES))
        video, doc = await asyncio.gather(files.count_documents({"file_type": "video"}),
                                          files.count_documents({"file_type": "document"}))
        size = 0
        async for r in files.aggregate([{"$group": {"_id": None, "s": {"$sum": "$file_size"}}}]):
            size = r["s"]
        return {"langs": langs, "quals": quals, "video": video, "doc": doc, "size": size}

    per_day, lib, tot = await asyncio.gather(S.indexed_per_day(d14), cached("library", 600, library), totals())

    async def one(cid):
        try:
            title = (await bot.get_chat(cid)).title
        except Exception:
            title = None
        total = await files.count_documents({"channel_id": cid})
        t = (await S.indexed_per_day([today], cid))[today]
        return cid, title, total, t

    chans = await asyncio.gather(*(one(c) for c in settings["index_channels"][:10]))
    in_channels = sum(c[2] for c in chans)
    last7 = sum(per_day[d] for d in d14[-7:])

    text = (
        "📁 <b>Files & Index</b>\n\n"
        f"<b>Library:</b> <code>{n_(tot['files'])}</code> files · <b>{mb(lib['size'])}</b>\n"
        f"🎬 Video <code>{n_(lib['video'])}</code> · 📄 Document <code>{n_(lib['doc'])}</code>\n\n"
        "<b>📚 Indexed</b>\n"
        f"Today <b>{n_(per_day[today])}</b> · Yesterday <b>{n_(per_day[yday])}</b> · "
        f"7 days <b>{n_(last7)}</b> · 14 days <b>{n_(sum(per_day.values()))}</b>\n"
        f"<code>{spark([per_day[d] for d in d14])}</code>  <i>last 14 days</i>\n\n"
        f"<b>📢 Index channels</b>  <i>({len(settings['index_channels'])})</i>\n"
    )
    if not chans:
        text += "<i>No index channels set.</i>\n"
    for i, (cid, title, total, t) in enumerate(chans, 1):
        text += f"{i}. {esc(title) if title else '<i>unreachable</i>'}\n    <code>{cid}</code> — <b>{n_(total)}</b> files · today <b>{n_(t)}</b>\n"
    other = max(tot["files"] - in_channels, 0)
    if other:
        text += f"➕ Other / older files: <b>{n_(other)}</b>\n"
    langs = sorted(zip(_LANGS, lib["langs"]), key=lambda kv: -kv[1])
    langs = [f"{l.title()} {n_(c)}" for l, c in langs if c][:8]
    quals = [f"{q} {n_(c)}" for q, c in zip(_QUALITIES, lib["quals"]) if c]
    if langs:
        text += "\n<b>🌐 By language</b>\n" + " · ".join(langs) + "\n"
    if quals:
        text += "<b>🎞 By quality</b>\n" + " · ".join(quals) + "\n"
    return text, Markup([nav_row()])


async def page_shortener(bot, code: str, tier: str, page: int):
    days = period_days(code)
    if S.ist_day() in days:
        await S.backfill_verify_today()
    summ = await S.verify_summary(days)
    settings = await get_settings()
    done_all = sum(v["done"] for v in summ.values())
    text = f"🔗 <b>Shortener — {period_title(code)}</b>\n\n"
    for t in (1, 2, 3):
        dom = (settings["shorteners"].get(str(t)) or {}).get("domain") or "not set"
        v = summ[t]
        conv = f" · conversion <b>{pct(v['done'], v['prompts'])}</b>" if v["prompts"] else ""
        text += (f"<b>Shortener {t}</b>  <code>{esc(dom)}</code>\n"
                 f"    ✅ <b>{n_(v['done'])}</b> done · 👤 <b>{n_(v['users'])}</b> users · shown <b>{n_(v['prompts'])}</b>{conv}\n")
    text += f"\n<b>Total:</b> <code>{n_(done_all)}</code> completions"
    if _EARN_PER_1000 > 0:
        text += f" · est. earnings <b>{_CURRENCY}{done_all * _EARN_PER_1000 / 1000:,.2f}</b> <i>(estimate)</i>"
    text += "\n"
    which = None if tier == "a" else int(tier)
    rows, total = await S.verify_events(days, which, page * 8, 8)
    label = "all shorteners" if which is None else f"Shortener {which}"
    text += f"\n<b>👤 Users who completed — {label}</b>  <i>({n_(total)})</i>\n"
    if not rows:
        text += "<i>Nobody has completed a shortener in this period.</i>\n"
    multi_day = len(days) > 1
    for i, r in enumerate(rows, start=page * 8 + 1):
        when = dt_label(r["ts"]) if multi_day else t_label(r["ts"])
        text += (f"\n<b>{i}.</b> {person(r['uid'], r.get('nm'), r.get('un'))}\n"
                 f"    🆔 <code>{r['uid']}</code> · 🔗 <b>Shortener {r['tier']}</b> · 🕒 {when}\n")
    tier_row = [Btn(("✅ " if tier == t else "") + lbl, callback_data=f"st#short#{code}#{t}#0")
                for t, lbl in (("a", "All"), ("1", "S 1"), ("2", "S 2"), ("3", "S 3"))]
    markup = [period_row("short", code, f"{tier}#0"), tier_row, *list_nav(f"st#short#{code}#{tier}", page, total, 8), nav_row()]
    return text, Markup(markup)


async def page_search(bot, code: str):
    days = period_days(code)
    ss, top, miss, top_all = await asyncio.gather(
        S.search_summary(days), S.top_queries(days, 8), S.top_queries(days, 8, misses_only=True),
        S.top_queries(days, 400))
    o = ss["by_outcome"]
    names = {"found": "Found it", "fuzzy": "Typo fixed", "relaxed": "Title found, tag missing", "titles": "Title buttons shown",
             "related": "Related titles only", "suggested": "Outside suggestions only", "none": "Nothing found"}
    text = (
        f"🔎 <b>Search — {period_title(code)}</b>\n\n"
        f"Searches: <b>{n_(ss['total'])}</b> · by <b>{n_(ss['searchers'])}</b> people\n"
        f"✅ Found: <b>{n_(ss['hits'])}</b> ({pct(ss['hits'], ss['total'])}) · ❌ Missed: <b>{n_(ss['misses'])}</b> ({pct(ss['misses'], ss['total'])})\n"
        f"📩 PM <b>{n_(ss['pm'])}</b> · 🏘 Groups <b>{n_(ss['grp'])}</b>\n\n"
    )
    for key, label in names.items():
        if o.get(key):
            text += f"• {label}: <b>{n_(o[key])}</b>\n"
    text += "\n<b>🔥 Most searched</b>\n"
    text += "".join(f"{i}. <code>{esc(r['q'][:40])}</code> — <b>{n_(r['n'])}</b>\n" for i, r in enumerate(top, 1)) or "<i>No searches yet.</i>\n"
    if miss:
        text += "\n<b>❌ Most missed</b> <i>(content to upload)</i>\n"
        text += "".join(f"{i}. <code>{esc(r['q'][:40])}</code> — <b>{n_(r['n'])}</b>\n" for i, r in enumerate(miss, 1))
    langs = lang_counts(top_all)[:5]
    if langs:
        text += "\n<b>🌐 Languages people ask for</b>\n" + " · ".join(f"{l.title()} {n_(c)}" for l, c in langs) + "\n"
    return text, Markup([period_row("search", code), nav_row()])


async def page_requests(bot, code: str):
    days = period_days(code)
    rq = await requests_count(days)
    pending_all = await request_col.count_documents({"status": "pending"})
    top = [r async for r in request_col.aggregate([
        {"$match": rq["match"]}, {"$group": {"_id": {"$toLower": "$query"}, "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}, {"$limit": 8}])]
    latest = await request_col.find(rq["match"]).sort("created_at", -1).limit(6).to_list(6)
    text = (
        f"📝 <b>Requests — {period_title(code)}</b>\n\n"
        f"Requests: <b>{n_(rq['total'])}</b> · from <b>{n_(rq['users'])}</b> people\n"
        f"⏳ Waiting for an answer (all time): <b>{n_(pending_all)}</b>\n"
    )
    for status, n in sorted(rq["by_status"].items(), key=lambda kv: -kv[1]):
        text += f"• {esc(str(status).replace('_', ' ').title())}: <b>{n_(n)}</b>\n"
    text += "\n<b>🔥 Most requested</b>\n"
    text += "".join(f"{i}. <code>{esc((r['_id'] or '')[:40])}</code> — <b>{n_(r['n'])}</b>\n" for i, r in enumerate(top, 1)) \
        or "<i>No requests in this period.</i>\n"
    if latest:
        text += "\n<b>🕒 Latest</b>\n"
        for r in latest:
            un = r.get("username")
            who = f'<a href="https://t.me/{esc(un)}">@{esc(un)}</a>' if un else f'<a href="tg://user?id={r["user_id"]}">user</a>'
            text += f"• <code>{esc((r.get('query') or '')[:36])}</code> — {who} <code>{r['user_id']}</code> · {t_label(r['created_at'])}\n"
    return text, Markup([period_row("req", code), nav_row()])


async def page_premium(bot):
    now = datetime.now(timezone.utc)
    settings = await get_settings()

    async def within(days):
        return await premium_col.count_documents({"expiry_time": {"$gt": now, "$lte": now + timedelta(days=days)}})

    active, d1, d3, d7, tot = await asyncio.gather(
        premium_col.count_documents({"expiry_time": {"$gt": now}}), within(1), within(3), within(7), totals())
    soon = await premium_col.find({"expiry_time": {"$gt": now}}).sort("expiry_time", 1).limit(6).to_list(6)
    names = await user_names([d["_id"] for d in soon])
    text = (
        "💎 <b>Premium</b>\n\n"
        f"Feature: {'🟢 ON' if settings.get('premium_enabled') else '🔴 OFF'}\n"
        f"Active members: <b>{n_(active)}</b> ({pct(active, tot['users'])} of users)\n\n"
        f"<b>⏳ Expiring soon</b>\nWithin 24 h <b>{n_(d1)}</b> · 3 days <b>{n_(d3)}</b> · 7 days <b>{n_(d7)}</b>\n"
    )
    if soon:
        text += "\n<b>Next to expire</b>\n"
    for i, d in enumerate(soon, 1):
        n = names.get(d["_id"]) or {}
        left = utc(d["expiry_time"]) - now
        text += (f"\n<b>{i}.</b> {person(d['_id'], n.get('name'), n.get('username'))}\n"
                 f"    🆔 <code>{d['_id']}</code> · ⏳ {readable_time(left.total_seconds())} left\n")
    return text, Markup([nav_row()])


async def page_groups(bot, code: str):
    days = period_days(code)
    a, _ = S.day_bounds(days[0])
    _, b = S.day_bounds(days[-1])
    tot, (busy, active_groups) = await asyncio.gather(totals(), S.group_activity(days, 6))
    added = await db["bot_groups"].count_documents({"added_at": {"$gte": a, "$lt": b}})
    left_now = await db["bot_groups"].count_documents({"left": True})
    gdocs = {}
    async for d in db["bot_groups"].find({"_id": {"$in": [r["_id"] for r in busy]}}):
        gdocs[d["_id"]] = d
    text = (
        f"🏘 <b>Groups — {period_title(code)}</b>\n\n"
        f"Total groups: <b>{n_(tot['groups'])}</b> · left / removed: <b>{n_(left_now)}</b>\n"
        f"🆕 Added in this period: <b>{n_(added)}</b>\n"
        f"💬 Active groups: <b>{n_(active_groups)}</b>\n\n<b>🔥 Busiest groups</b>\n"
    )
    if not busy:
        text += "<i>No group activity recorded in this period.</i>\n"
    for i, r in enumerate(busy, 1):
        doc = gdocs.get(r["_id"]) or {"_id": r["_id"], "title": str(r["_id"])}
        adder = doc.get("added_by") or {}
        text += f"\n<b>{i}.</b> {group_name(doc)}\n    🆔 <code>{r['_id']}</code> · 💬 <b>{n_(r['m'])}</b> messages\n"
        if adder:
            text += f"    👤 Added by {person(adder.get('id', 0), adder.get('name'), adder.get('username'))}\n"
    return text, Markup([period_row("grp", code), nav_row()])


async def media_kit() -> dict:
    """Everything the sponsor-facing numbers are made of (cached for a minute)."""
    async def load():
        d7, d28, d30 = S.days_back(7), S.days_back(28), S.days_back(30)
        tot, a_w, a_m, ser28, srch7, ss7, ver7, top, groups = await asyncio.gather(
            totals(), S.activity_totals(d7), S.activity_totals(d30), S.daily_series(d28), S.searches_per_day(d7),
            S.search_summary(d7), S.verify_summary(d7), S.top_queries(d7, 400), S.group_activity(d7, 1))
        dau7 = [ser28[d]["dau"] for d in d7]
        active_days = [v for v in dau7 if v] or [0]
        hours = S.hours_total({d: ser28[d] for d in d7})
        order = sorted(range(24), key=lambda h: -hours[h])[:3] if any(hours) else []
        wk: dict = {}
        for d, row in ser28.items():
            if row["dau"]:
                wk.setdefault(datetime.strptime(d, "%Y-%m-%d").strftime("%A"), []).append(row["dau"])
        best_day = max(wk, key=lambda k: sum(wk[k]) / len(wk[k])) if len(wk) >= 3 else None
        return {
            "tot": tot, "wau": a_w["users"], "mau": a_m["users"], "dau_avg": sum(active_days) / len(active_days),
            "searches_day": sum(srch7.values()) / 7, "found_rate": pct(ss7["hits"], ss7["total"]),
            "files_day": a_w["files"] / 7, "req_avg": a_w["req"] / 7,
            "verify_day": sum(v["done"] for v in ver7.values()) / 7, "hours": order, "best_day": best_day,
            "langs": lang_counts(top)[:5], "groups_active": groups[1], "msgs_day": a_w["msgs"] / 7,
        }
    return await cached("kit", 60, load)


def _hours_text(order: list) -> str:
    return ", ".join(f"{hour_label(h)}–{hour_label((h + 1) % 24)}" for h in sorted(order))


async def page_ads(bot):
    m = await media_kit()
    t = m["tot"]
    text = (
        "📣 <b>Ads & Sponsors</b>\n<i>The numbers sponsors and advertisers ask for.</i>\n\n"
        "<b>👥 Audience</b>\n"
        f"Total users <b>{n_(t['users'])}</b> · reachable <b>{n_(t['seen'] - t['blocked'])}</b>\n"
        f"Monthly active <b>{n_(m['mau'])}</b> · Weekly <b>{n_(m['wau'])}</b> · Daily (avg) <b>{n_(m['dau_avg'])}</b>\n"
        f"Stickiness (daily ÷ monthly): <b>{pct(m['dau_avg'], m['mau'])}</b>\n\n"
        "<b>📈 Engagement (per day, last 7 days)</b>\n"
        f"🔎 Searches <b>{n_(m['searches_day'])}</b> (found {m['found_rate']})\n"
        f"📥 File requesters <b>{n_(m['req_avg'])}</b> · files delivered <b>{n_(m['files_day'])}</b>\n"
        f"💬 Messages & taps <b>{n_(m['msgs_day'])}</b>\n"
        f"🔗 Shortener completions <b>{n_(m['verify_day'])}</b>\n\n"
        "<b>🏘 Reach</b>\n"
        f"Groups <b>{n_(t['groups'])}</b> ({n_(m['groups_active'])} active this week) · Premium <b>{n_(t['premium'])}</b>\n\n"
    )
    if m["hours"]:
        text += f"⏰ <b>Best time to post an ad (IST):</b> {_hours_text(m['hours'])}\n"
    if m["best_day"]:
        text += f"📅 <b>Busiest weekday:</b> {m['best_day']}\n"
    if m["langs"]:
        text += "🌐 <b>Audience interests:</b> " + ", ".join(f"{l.title()}" for l, _c in m["langs"]) + "\n"
    if _EARN_PER_1000 > 0:
        month = (await S.verify_summary(S.days_back(30)))
        done = sum(v["done"] for v in month.values())
        text += f"\n💰 Shortener earnings (last 30 days, estimate): <b>{_CURRENCY}{done * _EARN_PER_1000 / 1000:,.2f}</b>\n"
    text += "\n<i>Tap “Shareable summary” to get a clean message you can forward to a sponsor.</i>"
    rows = [
        [Btn("📋 Shareable summary", callback_data="st#ads_share"), Btn("📄 Export CSV", callback_data="st#ads_csv")],
        [Btn("📅 Daily Report", callback_data="st#rep#7")],
        nav_row(),
    ]
    return text, Markup(rows)


def share_text(m: dict) -> str:
    t = m["tot"]
    lines = [
        "📊 <b>Audience Snapshot</b>",
        f"<i>{datetime.now(timezone.utc).astimezone(IST).strftime('%d %b %Y')}</i>\n",
        f"👥 <b>{n_(t['users'])}</b> total users",
        f"🔥 <b>{n_(m['mau'])}</b> monthly active · <b>{n_(m['wau'])}</b> weekly · <b>{n_(m['dau_avg'])}</b> daily (avg)",
        f"🔎 ~<b>{n_(m['searches_day'])}</b> searches a day",
        f"📥 ~<b>{n_(m['files_day'])}</b> files delivered a day to <b>{n_(m['req_avg'])}</b> people",
        f"🏘 <b>{n_(t['groups'])}</b> groups",
    ]
    if m["hours"]:
        lines.append(f"⏰ Peak hours (IST): {_hours_text(m['hours'])}")
    if m["langs"]:
        lines.append("🌐 Top interests: " + ", ".join(l.title() for l, _c in m["langs"]))
    return "\n".join(lines)


async def build_report_rows(n: int) -> list:
    days = S.days_back(n)
    ser, idx, new, srch = await asyncio.gather(S.daily_series(days), S.indexed_per_day(days),
                                               S.new_users_per_day(days), S.searches_per_day(days))
    rows = []
    for d in days:
        s = ser[d]
        rows.append({"day": d, "dau": s["dau"], "req": s["req"], "files": s["files"], "new": new[d], "idx": idx[d],
                     "short": s["vd1"] + s["vd2"] + s["vd3"], "s1": s["vd1"], "s2": s["vd2"], "s3": s["vd3"],
                     "searches": srch[d], "msgs": s["msgs"]})
    return rows


async def page_report(bot, n: int):
    rows = await build_report_rows(n)
    head = f"{'Date':<6}{'DAU':>5}{'Req':>5}{'Files':>6}{'New':>5}{'Idx':>5}{'Shrt':>5}{'Srch':>5}"
    lines = [head]
    for r in reversed(rows):
        lines.append(f"{d_label(r['day']):<6}{k_(r['dau']):>5}{k_(r['req']):>5}{k_(r['files']):>6}{k_(r['new']):>5}"
                     f"{k_(r['idx']):>5}{k_(r['short']):>5}{k_(r['searches']):>5}")
    lines.append("─" * len(head))
    tot = {k: sum(r[k] for r in rows) for k in ("files", "new", "idx", "short", "searches")}
    avg = lambda k: sum(r[k] for r in rows) / max(len(rows), 1)
    lines.append(f"{'Total':<6}{'':>5}{'':>5}{k_(tot['files']):>6}{k_(tot['new']):>5}{k_(tot['idx']):>5}{k_(tot['short']):>5}{k_(tot['searches']):>5}")
    lines.append(f"{'Avg':<6}{k_(avg('dau')):>5}{k_(avg('req')):>5}{k_(avg('files')):>6}{k_(avg('new')):>5}{k_(avg('idx')):>5}"
                 f"{k_(avg('short')):>5}{k_(avg('searches')):>5}")
    text = (
        f"📅 <b>Daily Report — last {n} days</b>\n\n<pre>{esc(chr(10).join(lines))}</pre>\n"
        "<i>DAU active users · Req file requesters · Files sent · New users · Idx files indexed · "
        "Shrt shortener completions · Srch searches</i>"
    )
    since = await S.tracking_since()
    if since and rows[0]["day"] < since:
        text += f"\n<i>ℹ️ DAU / Req / Files / Shrt are tracked from {d_label(since)}; earlier days show 0.</i>"
    row = [Btn(("✅ " if n == v else "") + f"{v} Days", callback_data=f"st#rep#{v}") for v in (7, 14, 30)]
    return text, Markup([row, [Btn("📄 Export CSV", callback_data="st#ads_csv")], nav_row()])


async def page_server(bot):
    settings = await get_settings()
    t0 = time.time()
    try:
        await bot.get_me()
        bot_ms = (time.time() - t0) * 1000
    except Exception:
        bot_ms = None
    t0 = time.time()
    try:
        await db.command("ping")
        db_ms = (time.time() - t0) * 1000
    except Exception:
        db_ms = None
    lines = ["🖥 <b>Server</b>\n"]
    if psutil:
        proc = psutil.Process(os.getpid())
        proc.cpu_percent(None)
        psutil.cpu_percent(None)
        await asyncio.sleep(0.5)
        cpu_bot, cpu_sys = proc.cpu_percent(None), psutil.cpu_percent(None)
        vm = psutil.virtual_memory()
        try:
            disk = psutil.disk_usage("/")
            disk_line = f"💽 Disk <b>{mb(disk.used)}</b> / {mb(disk.total)} (<b>{disk.percent:.0f}%</b>)\n"
        except Exception:
            disk_line = ""
        lines += [
            f"⏱ Uptime: <b>{readable_time(time.time() - proc.create_time())}</b>",
            f"🧠 RAM — bot <b>{mb(proc.memory_info().rss)}</b> · server <b>{mb(vm.used)}</b> / {mb(vm.total)} (<b>{vm.percent:.0f}%</b>)",
            f"🔥 CPU — bot <b>{cpu_bot:.1f}%</b> · server <b>{cpu_sys:.1f}%</b> · {psutil.cpu_count()} core(s)",
            disk_line.rstrip(),
        ]
    else:
        lines += [f"⏱ Uptime: <b>{readable_time(time.time() - _PROC_START)}</b>", "ℹ️ Install <code>psutil</code> for RAM / CPU / disk."]
    lines += [
        "",
        "<b>⚡ Speed</b>",
        f"Telegram: <b>{bot_ms:.0f} ms</b>" if bot_ms is not None else "Telegram: ⚠️ not answering",
        f"MongoDB: <b>{db_ms:.0f} ms</b>" if db_ms is not None else "MongoDB: ⚠️ not answering",
        "",
        f"🐍 Python <code>{platform.python_version()}</code> · Pyrogram <code>{pyrogram.__version__}</code>",
        "",
        "<b>🎛 Features</b>",
    ]
    feats = [("Autofilter", "autofilter_enabled"), ("PM filter", "pm_filter_enabled"), ("Verification", "verify_enabled"),
             ("File limit", "file_limit_enabled"), ("Premium", "premium_enabled"), ("Force-sub", "force_sub_enabled"),
             ("Query auto-delete", "query_autodelete_enabled"), ("File auto-delete", "file_autodelete_enabled"),
             ("Welcome", "welcome_enabled"), ("Movie updates", "movie_update_notification"), ("AI learning", "ai_learning_enabled")]
    lines.append(" · ".join(f"{'🟢' if settings.get(key, False) else '🔴'} {name}" for name, key in feats))
    try:                                                   # fast download (Watch / Download buttons)
        from database.stream_db import daily_col as fd_daily, usage_col as fd_usage
        today = S.ist_day()
        users = await fd_daily.count_documents({"date": today})
        links = 0
        async for r in fd_daily.aggregate([{"$match": {"date": today}}, {"$group": {"_id": None, "n": {"$sum": "$count"}}}]):
            links = r["n"]
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        usage = [(d["_id"].split(":", 1)[-1], d.get("bytes", 0)) async for d in fd_usage.find({"_id": {"$regex": f"^{month}:"}})]
        lines += ["", "<b>⚡ Fast download</b>", f"Today: <b>{n_(users)}</b> users · <b>{n_(links)}</b> links"]
        lines += [f"Bandwidth this month — {esc(host)}: <b>{mb(b)}</b>" for host, b in usage]
    except Exception:
        pass
    return "\n".join(x for x in lines if x is not None), Markup([nav_row()])


async def page_database(bot):
    u = await db_usage()
    if not u:
        return "🗄 <b>Database</b>\n\n⚠️ Couldn't read storage stats.", Markup([nav_row()])

    async def biggest():
        names = await db.list_collection_names()
        sem = asyncio.Semaphore(6)

        async def one(name):
            async with sem:
                try:
                    st = await db.command("collStats", name)
                    return name, st.get("storageSize", 0) + st.get("totalIndexSize", 0), st.get("count", 0)
                except Exception:
                    return name, 0, 0
        rows = await asyncio.gather(*(one(n) for n in names))
        return sorted(rows, key=lambda r: -r[1])[:8]

    big = await cached("biggest", 300, biggest)
    text = (
        "🗄 <b>Database</b>\n\n"
        f"{dot(u['pct'])} <code>{bar(int(u['pct']), 100, 12)}</code> <b>{u['pct']:.1f}%</b>\n"
        f"💽 Used <b>{mb(u['used'])}</b> of <b>{mb(u['limit'])}</b> · free <b>{mb(max(u['limit'] - u['used'], 0))}</b>\n"
        f"📄 Data {mb(u['data'])} · Indexes {mb(u['index'])}\n"
        f"📚 Collections <code>{u['collections']}</code> · Documents <code>{n_(u['objects'])}</code>\n\n"
        "<b>📦 Biggest collections</b>\n"
    )
    for i, (name, size, count) in enumerate(big, 1):
        text += f"{i}. <code>{esc(name)}</code> — <b>{mb(size)}</b> · {n_(count)} docs\n"
    text += (f"\n<i>Auto-clean: search log keeps {os.environ.get('QUERY_LOG_DAYS', '90')} days · "
             f"daily stats lists keep {S.KEEP_DAYS} days.</i>")
    if u["pct"] >= 80:
        text += "\n\n⚠️ <b>Storage is getting full.</b> Lower QUERY_LOG_DAYS / STATS_KEEP_DAYS, or upgrade the database plan."
    return text, Markup([nav_row()])


# ══════════════════════════════════════════════════════════════════════════════
# ROUTING + HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

_last_page: dict = {}


async def render(bot, data: str):
    parts = data.split("#")[1:]
    page = parts[0] if parts else "home"
    arg = lambda i, default="": parts[i] if len(parts) > i else default
    if page == "users":
        return await page_users(bot)
    if page == "act":
        return await page_activity(bot, arg(1, "t"), int(arg(2, "0") or 0))
    if page == "files":
        return await page_files(bot)
    if page == "short":
        return await page_shortener(bot, arg(1, "t"), arg(2, "a"), int(arg(3, "0") or 0))
    if page == "search":
        return await page_search(bot, arg(1, "t"))
    if page == "req":
        return await page_requests(bot, arg(1, "t"))
    if page == "prem":
        return await page_premium(bot)
    if page == "grp":
        return await page_groups(bot, arg(1, "t"))
    if page == "ads":
        return await page_ads(bot)
    if page == "rep":
        return await page_report(bot, int(arg(1, "7") or 7))
    if page == "srv":
        return await page_server(bot)
    if page == "dbs":
        return await page_database(bot)
    return await page_home(bot)


@Client.on_message(filters.command("stats") & filters.private & ADMIN)
async def stats_cmd(bot, message):
    wait = await message.reply_text("📊 Loading the dashboard…")
    _last_page[message.from_user.id] = "st#home"
    text, markup = await page_home(bot)
    await safe_edit(wait, text, markup)


@Client.on_message(filters.command("stats") & ~filters.private & ADMIN)
async def stats_in_group(_, message):
    await message.reply_text("🔒 Use /stats in my private chat — it shows user names and ids.")


@Client.on_callback_query(filters.regex(r"^st#") & ADMIN)
async def stats_callbacks(bot, query):
    data = query.data
    admin = query.from_user.id
    action = data.split("#")[1]
    if action == "noop":
        await query.answer()
        return
    if action == "close":
        await query.answer()
        try:
            await query.message.delete()
        except RPCError:
            pass
        return
    if action in ("ads_share", "ads_csv"):
        await query.answer("Preparing…")
        chat = query.message.chat.id
        if action == "ads_share":
            await bot.send_message(chat, share_text(await media_kit()), disable_web_page_preview=True)
            await bot.send_message(chat, "☝️ Forward the message above to your sponsor.")
            return
        rows = await build_report_rows(30)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "active_users", "file_requesters", "files_sent", "new_users", "files_indexed",
                    "shortener_total", "shortener_1", "shortener_2", "shortener_3", "searches", "messages"])
        for r in rows:
            w.writerow([r["day"], r["dau"], r["req"], r["files"], r["new"], r["idx"], r["short"], r["s1"], r["s2"], r["s3"],
                        r["searches"], r["msgs"]])
        out = io.BytesIO(buf.getvalue().encode("utf-8"))
        out.name = f"bot-stats-{S.ist_day()}.csv"
        await bot.send_document(chat, out, caption="📄 Daily numbers for the last 30 days.")
        return
    if action == "rf":
        _cache.clear()
        data = _last_page.get(admin, "st#home")
        await query.answer("Refreshed")
    else:
        await query.answer()
    _last_page[admin] = data
    try:
        text, markup = await render(bot, data)
    except Exception:
        logger.exception("stats page failed: %s", data)
        text, markup = "❌ Couldn't load this page — see the logs.", Markup([nav_row()])
    await safe_edit(query.message, text, markup)


# ── who is active today (feeds the Activity / Users / Ads pages) ──────────────

def _name_of(u) -> str:
    return " ".join(p for p in (u.first_name, u.last_name) if p) or "User"


@Client.on_message((filters.private | filters.group) & ~filters.service, group=-4)
async def track_message(_, message):
    u = message.from_user
    if not u or u.is_bot:
        return
    in_group = message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
    S.record_activity(u.id, _name_of(u), u.username, message.chat.id if in_group else None, in_group)


@Client.on_callback_query(group=-4)
async def track_tap(_, query):
    u = query.from_user
    if not u or u.is_bot:
        return
    chat = query.message.chat if query.message else None
    in_group = bool(chat and chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP))
    S.record_activity(u.id, _name_of(u), u.username, chat.id if in_group else None, in_group)
