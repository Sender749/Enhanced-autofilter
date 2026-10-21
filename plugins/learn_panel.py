"""
/settings -> 🧠 AI Learning — switch the AI on/off and correct it when it
misbehaves. Also reachable with /ailearn (and `/ailearn run` to look for
suggestions right now).

  * on/off for the whole thing, for each kind of suggestion, and for each
    signal it learns from
  * how many different users must agree before it suggests anything
  * ⚠️ Report a problem: check a search that gave wrong results or was
    misunderstood (shows every alias / filter word / ignore word that touched
    it, each with a one-tap remove), or make the AI stricter / looser
  * recent approvals with Undo, the blocklist, top missing titles, reset
"""
import hashlib
import html
import logging

from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ADMINS
from database import learn_db as L
from database.alias_db import alias_key, get_alias, delete_alias
from database.filters_db import _tokenize, get_noise_words, related_titles, search_files
from database.settings_db import get_settings, update_settings
from plugins.settings import _safe_delete, _wait_for_admin

logger = logging.getLogger(__name__)
HTML = enums.ParseMode.HTML
ADMIN = filters.user(ADMINS)

TOGGLES = {
    "ai_learning_enabled", "ai_type_alias", "ai_type_filterword", "ai_type_noise",
    "ai_learn_retry", "ai_learn_taps", "ai_learn_requests", "ai_use_llm",
}
NUMBERS = {"ai_min_users": (1, 10, 1), "ai_daily_cap": (5, 50, 5)}  # field: (min, max, step)
_CHECK_ALIASES: dict = {}  # token -> alias key, for the "remove this alias" buttons


def _tick(flag) -> str:
    return "✅" if flag else "❌"


def _back(to: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"ail#{to}")]])


# ── screens ─────────────────────────────────────────────────────────────────

async def render_home():
    s = await get_settings()
    counts = await L.state_counts()
    blocked = await L.blocked_count()
    observed = await L.event_count()
    posted_today = await L.posted_since(24)
    on = s["ai_learning_enabled"]
    text = (
        f"🧠 <b>AI Learning</b> — {'🟢 ON' if on else '🔴 OFF'}\n\n"
        "The bot watches searches that failed (no names stored) and suggests aliases, filter words and "
        "title clean-ups in the not-found channel. <b>Nothing changes until an admin taps Approve.</b>\n\n"
        f"📊 Observed: {observed} · ⏳ Waiting: {counts['posted']} · ✅ Approved: {counts['approved']} · "
        f"🚫 Rejected: {counts['rejected']}\n"
        f"📬 Posted today: {posted_today}/{s['ai_daily_cap']} · 🔒 Blocked: {blocked}"
    )
    n = s["ai_min_users"]
    rows = [
        [InlineKeyboardButton("🔴 Turn OFF" if on else "🟢 Turn ON", callback_data="ail#t#ai_learning_enabled")],
        [InlineKeyboardButton(f"{_tick(s['ai_type_alias'])} Aliases", callback_data="ail#t#ai_type_alias"),
         InlineKeyboardButton(f"{_tick(s['ai_type_filterword'])} Filter words", callback_data="ail#t#ai_type_filterword"),
         InlineKeyboardButton(f"{_tick(s['ai_type_noise'])} Ignore words", callback_data="ail#t#ai_type_noise")],
        [InlineKeyboardButton(f"{_tick(s['ai_learn_retry'])} Retries", callback_data="ail#t#ai_learn_retry"),
         InlineKeyboardButton(f"{_tick(s['ai_learn_taps'])} Taps", callback_data="ail#t#ai_learn_taps"),
         InlineKeyboardButton(f"{_tick(s['ai_learn_requests'])} Requests", callback_data="ail#t#ai_learn_requests")],
        [InlineKeyboardButton(f"{_tick(s['ai_use_llm'])} AI judge (Groq/Gemini/TMDB)", callback_data="ail#t#ai_use_llm")],
        [InlineKeyboardButton("➖", callback_data="ail#n#ai_min_users#-1"),
         InlineKeyboardButton(f"👥 Users must agree: {n}", callback_data="ail#home"),
         InlineKeyboardButton("➕", callback_data="ail#n#ai_min_users#1")],
        [InlineKeyboardButton("➖", callback_data="ail#n#ai_daily_cap#-1"),
         InlineKeyboardButton(f"📬 Max per day: {s['ai_daily_cap']}", callback_data="ail#home"),
         InlineKeyboardButton("➕", callback_data="ail#n#ai_daily_cap#1")],
        [InlineKeyboardButton("⚠️ Report a problem", callback_data="ail#prob")],
        [InlineKeyboardButton("↩️ Recent approvals", callback_data="ail#recent"),
         InlineKeyboardButton("📈 Top missing", callback_data="ail#top")],
        [InlineKeyboardButton(f"🔒 Blocklist ({blocked})", callback_data="ail#blk"),
         InlineKeyboardButton("🗑 Reset learned data", callback_data="ail#rst")],
        [InlineKeyboardButton("⬅️ Back", callback_data="cfg#main")],
    ]
    return text, InlineKeyboardMarkup(rows)


async def render_recent():
    approved = await L.cands_in_state(["approved"], limit=8)
    if not approved:
        return "↩️ <b>Recent approvals</b>\n\nNothing approved yet.", _back()
    icons = {"alias": "🔀", "filterword": "🔤", "noise": "🧹"}
    lines, buttons = [], []
    for i, c in enumerate(approved, 1):
        what = f"{html.escape(c['key'])} → {html.escape(c['title'])}" if c["type"] == "alias" else html.escape(c["key"])
        lines.append(f"{i}. {icons.get(c['type'], '•')} {what}")
        buttons.append(InlineKeyboardButton(f"🗑 Undo {i}", callback_data=f"ail#undo#{c['_id']}"))
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="ail#home")])
    text = ("↩️ <b>Recent approvals</b>\n\n" + "\n".join(lines) +
            "\n\nUndo removes it and the AI won't suggest it again.")
    return text, InlineKeyboardMarkup(rows)


async def render_top():
    top = await L.top_missing(10)
    if not top:
        return "📈 <b>Top missing titles</b>\n\nNo failed searches recorded yet.", _back()
    lines = [f"{i}. <code>{html.escape(q)}</code> — {users} user(s)" + (f", {req} request(s)" if req else "")
             for i, (q, users, req) in enumerate(top, 1)]
    return ("📈 <b>Top missing titles</b> (last 30 days)\n\n" + "\n".join(lines) +
            "\n\nThese are searches that found nothing — good candidates to add to the database."), _back()


def render_problem():
    text = ("⚠️ <b>Report a problem</b>\n\nWhat went wrong?\n\n"
            "• A search showed wrong results, or the bot misunderstood it → check that search.\n"
            "• Too many / unhelpful suggestions → make the AI stricter.\n"
            "• It suggests too little → make it looser.")
    rows = [
        [InlineKeyboardButton("🔎 Check a search that went wrong", callback_data="ail#chk")],
        [InlineKeyboardButton("📉 Too many suggestions", callback_data="ail#strict"),
         InlineKeyboardButton("📈 Too few", callback_data="ail#loose")],
        [InlineKeyboardButton("⬅️ Back", callback_data="ail#home")],
    ]
    return text, InlineKeyboardMarkup(rows)


async def analyze_query(q: str):
    """Everything that changes how this search is understood, with a way to remove each."""
    e = html.escape
    qkey = alias_key(q)
    s = await get_settings()
    lines, buttons = [f"🔎 <b>Search:</b> <code>{e(q)}</code>\n"], []

    alias = await get_alias(qkey) if qkey else None
    if alias:
        cand = await L.cand_find("alias", qkey, alias)
        lines.append(f"🔀 Alias: <code>{e(qkey)}</code> → <b>{e(alias)}</b>" + (" (AI)" if cand and cand["state"] == "approved" else ""))
        if cand and cand["state"] == "approved":
            buttons.append(InlineKeyboardButton("🗑 Remove alias", callback_data=f"ail#undo#{cand['_id']}"))
        else:
            token = hashlib.md5(qkey.encode()).hexdigest()[:10]
            _CHECK_ALIASES[token] = qkey
            buttons.append(InlineKeyboardButton("🗑 Remove alias", callback_data=f"ail#da#{token}"))

    low = q.lower()
    for w in s["filter_words"]:
        if w.lower() in low:
            cand = await L.cand_find("filterword", w.lower())
            tag = " (AI)" if cand and cand["state"] == "approved" else ""
            lines.append(f"🔤 Filter word dropped: <code>{e(w)}</code>{tag}")
            if tag:
                buttons.append(InlineKeyboardButton(f"🗑 Remove '{w[:12]}'", callback_data=f"ail#undo#{cand['_id']}"))
    for w in _tokenize(q):
        if w in get_noise_words():
            cand = await L.cand_find("noise", w)
            lines.append(f"🧹 Ignored at start of titles: <code>{e(w)}</code>")
            if cand and cand["state"] == "approved":
                buttons.append(InlineKeyboardButton(f"🗑 Remove '{w[:12]}'", callback_data=f"ail#undo#{cand['_id']}"))

    if len(lines) == 1:
        lines.append("No alias, filter word or ignore word touches this search.")
    results = await search_files(q)
    if results:
        lines.append(f"\n📁 The search finds <b>{len(results)}</b> file(s).")
    else:
        related = await related_titles(q, 3)
        lines.append("\n📁 The search finds nothing." + (f" Related: {e(', '.join(related))}" if related else ""))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("🔎 Check another", callback_data="ail#chk"),
                 InlineKeyboardButton("⬅️ Back", callback_data="ail#prob")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ── buttons ─────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^ail#(home|t|n|recent|prob|chk|strict|loose|top|blk|blkclr|rst|rstok|da)(#|$)"))
async def panel_buttons(bot, query):
    if query.from_user.id not in ADMINS:
        await query.answer("Admins only.", show_alert=True)
        return
    parts = query.data.split("#")
    action = parts[1]
    msg = query.message

    async def show(text, markup):
        try:
            await msg.edit_text(text, parse_mode=HTML, reply_markup=markup)
        except Exception:
            pass  # "message is not modified" when nothing changed

    if action == "home":
        await show(*await render_home())
        return await query.answer()

    if action == "t" and parts[2] in TOGGLES:
        s = await get_settings()
        await update_settings({parts[2]: not s[parts[2]]})
        await show(*await render_home())
        return await query.answer()

    if action == "n" and parts[2] in NUMBERS:
        lo, hi, step = NUMBERS[parts[2]]
        s = await get_settings()
        new = max(lo, min(hi, s[parts[2]] + int(parts[3]) * step))
        await update_settings({parts[2]: new})
        await show(*await render_home())
        return await query.answer(str(new))

    if action == "recent":
        await show(*await render_recent())
        return await query.answer()

    if action == "top":
        await show(*await render_top())
        return await query.answer()

    if action == "prob":
        await show(*render_problem())
        return await query.answer()

    if action in ("strict", "loose"):
        s = await get_settings()
        sign = 1 if action == "strict" else -1
        users = max(1, min(10, s["ai_min_users"] + sign))
        cap = max(5, min(50, s["ai_daily_cap"] - 5 * sign))
        await update_settings({"ai_min_users": users, "ai_daily_cap": cap})
        await show(*await render_home())
        return await query.answer(f"Users must agree: {users} · max per day: {cap}", show_alert=True)

    if action == "blk":
        n = await L.blocked_count()
        rows = [[InlineKeyboardButton("🧽 Clear blocklist", callback_data="ail#blkclr")]] if n else []
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="ail#home")])
        await show(f"🔒 <b>Blocklist</b>\n\n{n} suggestion(s) that were rejected or undone and won't be "
                   "suggested again. Clearing it lets the AI suggest them again if users still behave that way.",
                   InlineKeyboardMarkup(rows))
        return await query.answer()

    if action == "blkclr":
        cleared = await L.clear_blocked()
        await show(*await render_home())
        return await query.answer(f"Cleared {cleared}.")

    if action == "rst":
        await show("🗑 <b>Reset learned data</b>\n\nThis forgets what the AI has observed and every suggestion "
                   "still waiting for review. Approved aliases / filter words / ignore words stay (undo those "
                   "from Recent approvals).\n\nContinue?",
                   InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, reset", callback_data="ail#rstok"),
                                          InlineKeyboardButton("❌ No", callback_data="ail#home")]]))
        return await query.answer()

    if action == "rstok":
        ev, pend = await L.reset_learning()
        await show(*await render_home())
        return await query.answer(f"Forgot {ev} observations and {pend} pending suggestions.", show_alert=True)

    if action == "da":
        key = _CHECK_ALIASES.pop(parts[2], None)
        if key and await delete_alias(key):
            await L.block("alias", key, "")
            await query.answer("Alias removed.")
        else:
            await query.answer("Alias already gone.", show_alert=True)
        return await show(*render_problem())

    if action == "chk":
        prompt = await msg.edit_text(
            "🔎 <b>Check a search</b>\n\nSend the search that gave wrong results or was misunderstood "
            "(exactly as the user typed it).", parse_mode=HTML, reply_markup=_back("prob"))
        await query.answer()
        reply, outcome = await _wait_for_admin(bot, msg.chat.id, query.from_user.id, 90)
        if outcome == "cancel":
            return
        if outcome == "timeout":
            await show(*render_problem())
            return
        text = (reply.text or "").strip()
        await _safe_delete(reply)
        if not text or text.startswith("/"):
            await show(*render_problem())
            return
        result_text, result_markup = await analyze_query(text)
        await prompt.edit_text(result_text, parse_mode=HTML, reply_markup=result_markup)


# ── /ailearn ────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("ailearn") & filters.private & ADMIN)
async def ailearn_cmd(bot, message):
    if len(message.command) > 1 and message.command[1].lower() == "run":
        from plugins.learn import run_cycle
        posted = await run_cycle(bot)
        return await message.reply_text(
            f"🧠 Looked for new suggestions — posted {posted}." if posted
            else "🧠 Nothing new to suggest right now (or the daily limit is reached).")
    text, markup = await render_home()
    await message.reply_text(text, parse_mode=HTML, reply_markup=markup)
